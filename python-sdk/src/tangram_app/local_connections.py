"""Developer connections for standalone connector execution (bring-your-own token).

`tangram-app connect <app> --token …` stores a developer-supplied OAuth
access token under `~/.tangram/connections/`; `call --connected` then
executes the connector's actions directly against the vendor endpoint
declared in `manifests/api/spec.pkl`, rendering the manifest's auth header
templates with that token. The platform's OAuth broker, publisher
credentials, and multi-user connection model stay in Tangram OS — this
lane is single-developer test tooling, still governed: the manifest's host
allowlist applies, the endpoint must be https (loopback excepted, for
tests), overriding the endpoint requires `endpointOverridable = true`, and
the default read-only policy gates mutations exactly as for apps.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .local_store import tangram_home
from .pkl import PklEvaluator

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class LocalConnectionError(ValueError):
    """A connection operation failed for a caller-actionable reason."""


def connections_root() -> Path:
    return tangram_home() / "connections"


def save_connection(app_id: str, token: str, tenant: str | None = None) -> Path:
    if not token or not token.strip():
        raise LocalConnectionError("token must be non-empty")
    target = connections_root() / f"{app_id.replace('/', '__')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": app_id,
        "token": token.strip(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    if tenant is not None:
        entry["tenant"] = tenant
    target.write_text(json.dumps(entry), encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # the token is a live credential
    return target


def load_connection(app_id: str) -> dict | None:
    target = connections_root() / f"{app_id.replace('/', '__')}.json"
    try:
        entry = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and entry.get("token") else None


def delete_connection(app_id: str) -> bool:
    target = connections_root() / f"{app_id.replace('/', '__')}.json"
    try:
        target.unlink()
        return True
    except OSError:
        return False


def load_connector_spec(package_root: str | Path) -> dict:
    """Evaluate `manifests/api/spec.pkl` and return the connector fields."""
    root = Path(package_root).resolve()
    module = root / "manifests" / "api" / "spec.pkl"
    if not module.is_file():
        raise LocalConnectionError(f"{root} has no manifests/api/spec.pkl")
    spec = PklEvaluator().evaluate(
        module, expression=None, root_dir=root, project_dir=root / "manifests"
    )
    if not isinstance(spec, dict) or not isinstance(spec.get("endpoint"), str):
        raise LocalConnectionError(
            "api/spec.pkl declares no connector endpoint — is this a connector package?"
        )
    return spec


def render_connection(
    spec: dict, connection: dict, *, endpoint_override: str | None = None
) -> tuple[str, dict[str, str]]:
    """Resolve (endpoint, auth headers) for one connected invocation."""
    endpoint = spec["endpoint"]
    if endpoint_override is not None:
        if spec.get("endpointOverridable") is not True:
            raise LocalConnectionError(
                "this connector declares endpointOverridable = false"
            )
        endpoint = endpoint_override
    host = urlsplit(endpoint).hostname or ""
    scheme = urlsplit(endpoint).scheme
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if scheme != "https" and not loopback:
        raise LocalConnectionError(f"connector endpoint must be https, got {endpoint!r}")
    allowlist = spec.get("endpointHostAllowlist") or []
    if endpoint_override is None and allowlist and not _host_allowed(host, allowlist):
        raise LocalConnectionError(
            f"endpoint host {host!r} is not in endpointHostAllowlist {allowlist}"
        )
    values = {"oauth.accessToken": connection["token"]}
    if connection.get("tenant"):
        values["oauth.tenantId"] = connection["tenant"]
    headers: dict[str, str] = {}
    auth = spec.get("auth") or {}
    for name, template in (auth.get("httpHeaders") or {}).items():
        text = template.get("template") if isinstance(template, dict) else template
        if not isinstance(text, str):
            raise LocalConnectionError(f"auth header {name!r} has no template")
        headers[name] = _render(text, values, header=name)
    if not headers:
        raise LocalConnectionError("connector declares no auth.httpHeaders to render")
    return endpoint, headers


def _render(template: str, values: dict[str, str], *, header: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise LocalConnectionError(
                f"auth header {header!r} needs {{{{{key}}}}} which this developer "
                "connection does not carry (supported: oauth.accessToken, "
                "oauth.tenantId via connect --tenant)"
            )
        return values[key]

    return _PLACEHOLDER.sub(substitute, template)


def _host_allowed(host: str, allowlist: list) -> bool:
    for pattern in allowlist:
        if not isinstance(pattern, str):
            continue
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
        if host == pattern:
            return True
    return False
