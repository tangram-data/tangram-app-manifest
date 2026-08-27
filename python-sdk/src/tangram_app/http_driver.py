"""Bounded OpenAPI request rendering and HTTP execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import re
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .errors import DriverError, HttpResponseError, RequestRenderError
from .models import Action, ActionBinding


_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")
_RESERVED_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True, slots=True)
class RenderedHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


class OpenApiRequestRenderer:
    """Reverse a compiled binding's flat arguments into an HTTP request."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_url_length: int = 8192,
        max_header_bytes: int = 16 * 1024,
        max_request_bytes: int = 1024 * 1024,
    ) -> None:
        self._base = _validate_base_url(base_url)
        self._headers = _validate_fixed_headers(headers or {})
        self.max_url_length = _positive(max_url_length, "max_url_length")
        self.max_header_bytes = _positive(max_header_bytes, "max_header_bytes")
        self.max_request_bytes = _positive(max_request_bytes, "max_request_bytes")

    @property
    def hostname(self) -> str:
        assert self._base.hostname is not None
        return self._base.hostname

    def render(self, binding: ActionBinding, arguments: Any) -> RenderedHttpRequest:
        if not isinstance(arguments, Mapping):
            raise RequestRenderError("HTTP action arguments must be an object")
        path = binding.path
        query: list[tuple[str, str]] = []
        rendered_headers: dict[str, str] = {}
        body_fields: dict[str, Any] = {}
        whole_body: Any = _MISSING

        for exposed_name, value in arguments.items():
            input_binding = binding.input_bindings.get(exposed_name)
            if input_binding is None:
                raise RequestRenderError(
                    f"binding {binding.id!r} has no HTTP input mapping for {exposed_name!r}"
                )
            location, name = input_binding.location, input_binding.name
            if location == "path":
                assert name is not None
                token = "{" + name + "}"
                if token not in path:
                    raise RequestRenderError(
                        f"path parameter {name!r} is not present in {path!r}"
                    )
                path = path.replace(token, _path_value(value, name))
            elif location == "query":
                assert name is not None
                query.extend(_query_values(name, value))
            elif location == "header":
                assert name is not None
                _validate_header_name(name)
                rendered_headers[name] = _header_value(value, name)
            elif location == "body":
                if name is None:
                    if body_fields or whole_body is not _MISSING:
                        raise RequestRenderError(
                            "binding mixes whole-body and body-field inputs"
                        )
                    whole_body = value
                else:
                    if whole_body is not _MISSING:
                        raise RequestRenderError(
                            "binding mixes whole-body and body-field inputs"
                        )
                    body_fields[name] = value
            else:  # Defensive for graphs constructed without from_dict.
                raise RequestRenderError(
                    f"unsupported HTTP input location {location!r}"
                )

        unresolved = _PATH_PARAMETER.findall(path)
        if unresolved:
            raise RequestRenderError(
                f"missing path parameter(s): {', '.join(unresolved)}"
            )

        body_value = whole_body if whole_body is not _MISSING else body_fields
        has_body = (
            whole_body is not _MISSING or bool(body_fields) or binding.body_required
        )
        body = _json_bytes(body_value) if has_body else None
        if body is not None and len(body) > self.max_request_bytes:
            raise RequestRenderError(
                f"HTTP request body exceeds {self.max_request_bytes} byte limit"
            )

        configured_names = {name.lower() for name in self._headers}
        headers = {
            name: value
            for name, value in rendered_headers.items()
            if name.lower() not in configured_names
        }
        headers.update(
            self._headers
        )  # Configured credentials cannot be agent-overridden.
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")
        _check_header_size(headers, self.max_header_bytes)

        base_path = self._base.path.rstrip("/")
        relative_path = path if path.startswith("/") else "/" + path
        url = urlunsplit(
            (
                self._base.scheme,
                self._base.netloc,
                base_path + relative_path,
                urlencode(query, doseq=True),
                "",
            )
        )
        if len(url.encode("utf-8")) > self.max_url_length:
            raise RequestRenderError(
                f"HTTP URL exceeds {self.max_url_length} byte limit"
            )
        return RenderedHttpRequest(binding.method, url, headers, body)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpExecutionDriver:
    """Execute compiled HTTP bindings with explicit network and size bounds.

    Loopback is the default. Set ``allow_remote=True`` only when the configured
    base URL itself is an explicitly trusted connector endpoint. Redirects and
    ambient proxy configuration are disabled in both modes.
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_remote: bool = False,
        timeout_seconds: float = 15.0,
        max_url_length: int = 8192,
        max_header_bytes: int = 16 * 1024,
        max_request_bytes: int = 1024 * 1024,
        max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.renderer = OpenApiRequestRenderer(
            base_url,
            headers=headers,
            max_url_length=max_url_length,
            max_header_bytes=max_header_bytes,
            max_request_bytes=max_request_bytes,
        )
        if not allow_remote and not _is_loopback(self.renderer.hostname):
            raise DriverError(
                "remote HTTP backends are disabled; configure a loopback URL or set "
                "allow_remote=True for an explicitly trusted connector endpoint"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    async def invoke(
        self, action: Action, binding: ActionBinding, arguments: Any
    ) -> Any:
        rendered = self.renderer.render(binding, arguments)
        return await asyncio.to_thread(self._invoke_sync, rendered)

    def _invoke_sync(self, rendered: RenderedHttpRequest) -> Any:
        request = Request(
            rendered.url,
            data=rendered.body,
            headers=dict(rendered.headers),
            method=rendered.method,
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as error:
            _read_bounded(error, self.max_response_bytes)
            raise HttpResponseError(
                error.code,
                "upstream request failed",
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise DriverError("HTTP transport error") from error
        with response:
            payload = _read_bounded(response, self.max_response_bytes)
            return _decode_response(payload, response.headers.get("Content-Type"))


class LocalHttpDriver(HttpExecutionDriver):
    """Loopback-only HTTP execution driver."""

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        if kwargs.pop("allow_remote", False):
            raise ValueError("LocalHttpDriver cannot enable remote access")
        super().__init__(base_url, allow_remote=False, **kwargs)


def _validate_base_url(value: str):
    if not isinstance(value, str) or not value:
        raise ValueError("base_url must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    return parsed


def _validate_fixed_headers(value: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ValueError("configured HTTP headers must be strings")
        _validate_header_name(name, allow_authorization=True)
        _validate_header_value(item, name)
        result[name] = item
    return result


def _validate_header_name(name: str, *, allow_authorization: bool = False) -> None:
    if not name or any(character in name for character in "\r\n:"):
        raise RequestRenderError(f"invalid HTTP header name {name!r}")
    if name.lower() in _RESERVED_HEADERS and not (
        allow_authorization and name.lower() == "authorization"
    ):
        raise RequestRenderError(f"HTTP header {name!r} is reserved")


def _validate_header_value(value: str, name: str) -> None:
    if "\r" in value or "\n" in value:
        raise RequestRenderError(f"HTTP header {name!r} contains a newline")


def _path_value(value: Any, name: str) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered = ",".join(_scalar(item, name) for item in value)
    else:
        rendered = _scalar(value, name)
    if "/" in rendered or "\\" in rendered or rendered in {".", ".."}:
        raise RequestRenderError(
            f"path parameter {name!r} contains a path separator or traversal segment"
        )
    return quote(rendered, safe="")


def _query_values(name: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        raise RequestRenderError(
            f"query parameter {name!r} cannot be an object in this SDK version"
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [(name, _scalar(item, name)) for item in value]
    return [(name, _scalar(value, name))]


def _header_value(value: Any, name: str) -> str:
    if isinstance(value, Mapping):
        raise RequestRenderError(f"header parameter {name!r} cannot be an object")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered = ",".join(_scalar(item, name) for item in value)
    else:
        rendered = _scalar(value, name)
    _validate_header_value(rendered, name)
    return rendered


def _scalar(value: Any, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise RequestRenderError(
        f"HTTP parameter {name!r} must be a scalar or array of scalars"
    )


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RequestRenderError(
            f"HTTP request body is not valid JSON: {error}"
        ) from error


def _check_header_size(headers: Mapping[str, str], limit: int) -> None:
    size = sum(
        len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
        for name, value in headers.items()
    )
    if size > limit:
        raise RequestRenderError(f"HTTP headers exceed {limit} byte limit")


def _read_bounded(stream, limit: int) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise DriverError(f"HTTP response exceeds {limit} byte limit")
    return payload


def _decode_response(payload: bytes, content_type: str | None) -> Any:
    if not payload:
        return None
    if _is_json_content_type(content_type):
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DriverError(
                f"HTTP response contains invalid JSON: {error}"
            ) from error
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DriverError("binary HTTP responses are not supported yet") from error


def _is_json_content_type(value: str | None) -> bool:
    if value is None:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _positive(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


_MISSING = object()
