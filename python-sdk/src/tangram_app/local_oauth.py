"""Tier-2 local OAuth: run the connector's real authorization-code dance.

`tangram-app connect <app> --oauth` executes the manifest's `OAuth2AuthCode`
flow against the real vendor with a DEVELOPER-registered OAuth client
(bring your own client id/secret; vendors permit loopback redirect URIs
for native clients): loopback callback server, `state` + PKCE S256,
code exchange per `tokenAuthMethod`, tenant capture, and automatic
refresh inside the manifest's `refreshWindowSeconds` during
`call --connected`. This makes the oauth block itself testable — scopes,
consent, refresh quirks — while publisher credentials and multi-user
connections stay platform-only.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .local_connections import (
    LocalConnectionError,
    load_connector_spec,
    save_connection,
)
from .local_store import tangram_home

CALLBACK_PATH = "/oauth/callback"
_DONE_PAGE = b"<html><body>Connected. You can close this tab.</body></html>"


class LocalOAuthError(LocalConnectionError):
    """The local OAuth dance failed for a caller-actionable reason."""


# -- developer OAuth client -------------------------------------------------


def _client_path(app_id: str) -> Path:
    return tangram_home() / "oauth_clients" / f"{app_id.replace('/', '__')}.json"


def save_dev_client(app_id: str, client_id: str, client_secret: str | None) -> Path:
    target = _client_path(app_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {"clientId": client_id}
    if client_secret:
        entry["clientSecret"] = client_secret
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
        sink.write(json.dumps(entry))
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def load_dev_client(app_id: str) -> dict | None:
    try:
        entry = json.loads(_client_path(app_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and entry.get("clientId") else None


# -- authorization ----------------------------------------------------------


def _require_https(url: str, what: str) -> None:
    parts = urllib.parse.urlsplit(url)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not loopback:
        raise LocalOAuthError(f"{what} must be https, got {url!r}")


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server contract)
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        self.server.captured = dict(urllib.parse.parse_qsl(parts.query))  # type: ignore[attr-defined]
        self.server.event.set()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *args):
        pass


def run_authorization(
    oauth: dict,
    client_id: str,
    *,
    on_url: Callable[[str], None],
    timeout_seconds: float = 300.0,
) -> dict:
    """Drive authorize → loopback callback; returns {code, verifier?, redirect_uri, params}."""
    _require_https(oauth["authorizationUrl"], "authorizationUrl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Callback)
    server.captured = None  # type: ignore[attr-defined]
    server.event = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        state = secrets.token_urlsafe(24)
        verifier = None
        query = [
            ("response_type", "code"),
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("state", state),
        ]
        scopes = oauth.get("scopes") or []
        if scopes:
            query.append(("scope", " ".join(scopes)))
        if oauth.get("pkce"):
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            query += [("code_challenge", challenge), ("code_challenge_method", "S256")]
        for param in oauth.get("additionalAuthorizeParams") or []:
            if isinstance(param, dict) and param.get("name"):
                query.append((param["name"], param.get("value", "")))
        on_url(oauth["authorizationUrl"] + "?" + urllib.parse.urlencode(query))
        if not server.event.wait(timeout_seconds):  # type: ignore[attr-defined]
            raise LocalOAuthError(
                f"no OAuth callback within {int(timeout_seconds)}s — was the browser flow completed?"
            )
        params = server.captured or {}  # type: ignore[attr-defined]
        if params.get("error"):
            raise LocalOAuthError(
                f"vendor refused authorization: {params['error']} {params.get('error_description', '')}".strip()
            )
        if params.get("state") != state:
            raise LocalOAuthError("callback state mismatch — aborting (possible CSRF)")
        if not params.get("code"):
            raise LocalOAuthError("callback carried no authorization code")
        return {
            "code": params["code"],
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "params": params,
        }
    finally:
        server.shutdown()
        server.server_close()


def _token_request(oauth: dict, form: dict[str, str], client: dict) -> dict:
    _require_https(oauth["tokenUrl"], "tokenUrl")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    method = oauth.get("tokenAuthMethod") or "ClientSecretPost"
    secret = client.get("clientSecret")
    if method == "ClientSecretBasic" and secret:
        credentials = base64.b64encode(f"{client['clientId']}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    else:
        form["client_id"] = client["clientId"]
        if secret:
            form["client_secret"] = secret
    request = urllib.request.Request(
        oauth["tokenUrl"],
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise LocalOAuthError(f"token endpoint answered {error.code}: {detail}") from None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise LocalOAuthError("token endpoint returned no access_token")
    return payload


def _expires_at(payload: dict) -> str | None:
    expires_in = payload.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def _tenant_from(oauth: dict, params: dict) -> str | None:
    tenant = oauth.get("tenant")
    if isinstance(tenant, dict) and tenant.get("kind") == "CallbackParam":
        return params.get(tenant.get("name", ""))
    return None


def oauth_connect(
    package_root: str | Path,
    app_id: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    on_url: Callable[[str], None],
    timeout_seconds: float = 300.0,
) -> dict:
    """Full local dance for one connector; stores the connection; returns a
    token-free summary."""
    spec = load_connector_spec(package_root)
    oauth = spec.get("oauth")
    if not isinstance(oauth, dict) or not oauth.get("authorizationUrl"):
        raise LocalOAuthError("this connector declares no oauth block in api/spec.pkl")
    stored = load_dev_client(app_id) or {}
    client = {
        "clientId": client_id or stored.get("clientId"),
        "clientSecret": client_secret if client_secret is not None else stored.get("clientSecret"),
    }
    if not client["clientId"]:
        raise LocalOAuthError(
            "no developer OAuth client for this connector — pass --client-id "
            "(register your own native/desktop client with the vendor; loopback "
            "redirect URIs are permitted for those)"
        )
    save_dev_client(app_id, client["clientId"], client.get("clientSecret"))
    grant = run_authorization(
        oauth, client["clientId"], on_url=on_url, timeout_seconds=timeout_seconds
    )
    form = {
        "grant_type": "authorization_code",
        "code": grant["code"],
        "redirect_uri": grant["redirect_uri"],
    }
    if grant["verifier"]:
        form["code_verifier"] = grant["verifier"]
    payload = _token_request(oauth, form, client)
    tenant = _tenant_from(oauth, grant["params"])
    expires_at = _expires_at(payload)
    save_connection(
        app_id,
        payload["access_token"],
        tenant=tenant,
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
    )
    return {
        "expiresAt": expires_at,
        "refreshToken": bool(payload.get("refresh_token")),
        "tenant": tenant,
        "grantedScope": payload.get("scope"),
    }


def ensure_fresh(spec: dict, app_id: str, connection: dict) -> dict:
    """Refresh the stored token when inside the manifest's refresh window."""
    oauth = spec.get("oauth")
    expires_at = connection.get("expiresAt")
    refresh = connection.get("refreshToken")
    if not (isinstance(oauth, dict) and oauth.get("tokenUrl") and expires_at and refresh):
        return connection
    window = oauth.get("refreshWindowSeconds")
    window = window if isinstance(window, (int, float)) and window >= 0 else 60
    try:
        deadline = datetime.fromisoformat(expires_at) - timedelta(seconds=window)
    except ValueError:
        return connection
    if datetime.now(timezone.utc) < deadline:
        return connection
    client = load_dev_client(app_id)
    if client is None:
        raise LocalOAuthError(
            "token expired and no developer OAuth client is stored — reconnect with "
            "`tangram-app connect ... --oauth`"
        )
    payload = _token_request(
        oauth, {"grant_type": "refresh_token", "refresh_token": refresh}, client
    )
    save_connection(
        app_id,
        payload["access_token"],
        tenant=connection.get("tenant"),
        refresh_token=payload.get("refresh_token") or refresh,
        expires_at=_expires_at(payload),
    )
    from .local_connections import load_connection

    return load_connection(app_id) or connection
