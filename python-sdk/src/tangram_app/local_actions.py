"""Loopback action surface for locally hosted Python backends.

Mirrors the platform's backend SDK contract (composable-app-sdk design §5.3):
the staged ``tangram.actions.invoke`` composes the app's OWN governed actions
through the host pipeline, unattended — irreversible or confirmation-gated
actions refuse (there is no approval surface in backend code), and cross-app
targets refuse with an explicit "requires Tangram OS". Deliberately separate
from the browser ``/action`` bridge, whose confirmation relay is a direct-user
affordance a backend must never ride.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .policy import LocalDevelopmentPolicy

if TYPE_CHECKING:  # pragma: no cover
    from .local_runtime import LocalAppSession

_MAX_BODY = 1024 * 1024
PROTOCOL = "1"


def _err(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


class LocalActionsServer:
    """Threaded loopback server the staged backend SDK posts to."""

    def __init__(
        self, server: ThreadingHTTPServer, thread: threading.Thread, token: str
    ) -> None:
        self._server = server
        self._thread = thread
        self.url = f"http://127.0.0.1:{server.server_port}"
        # Per-session bearer secret: only the backend process we launched
        # (which receives it via env) may call this surface — not any local
        # process that discovers the port.
        self.token = token

    @classmethod
    def start(cls) -> "LocalActionsServer":
        holder = _SessionHolder()
        token = secrets.token_urlsafe(32)
        handler = _handler_factory(holder, token)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instance = cls(server, thread, token)
        instance._holder = holder
        return instance

    def attach(self, session: "LocalAppSession") -> None:
        self._holder.session = session

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _SessionHolder:
    session: "LocalAppSession | None" = None


def invoke_backend_action(
    session: "LocalAppSession",
    resource_type: str,
    action_name: str,
    arguments: Any,
) -> Any:
    """Unattended invocation of one of the app's own actions.

    Same resolution as the browser bridge, WITHOUT its confirmation relay:
    a gated action refuses outright, exactly like the platform backend lane.
    """
    matches = [
        action
        for action in session.app.graph.actions
        if action.resource_type == resource_type and action.name == action_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"app declares no unambiguous action {resource_type}.{action_name}"
        )
    action = matches[0]
    if action.effect.value == "Irreversible" or action.requires_confirmation:
        raise PermissionError(
            f"action '{resource_type}.{action_name}' needs user confirmation and "
            "cannot run from backend code"
        )
    policy = LocalDevelopmentPolicy(allow_mutations={action.id})
    bound = session.app.bind(
        backend=session.backend_url,
        policy=policy,
        # Keep the session's audit trail: backend-composed actions must not
        # run unrecorded just because the policy is swapped per call.
        audit_path=getattr(session, "audit_path", None),
        timeout_seconds=session.request_timeout_seconds,
    )
    return asyncio.run(bound.call(action.id, arguments))


def _handler_factory(holder: _SessionHolder, token: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server contract)
            asked = self.headers.get("X-Tangram-SDK-Protocol")
            if asked and asked.split(".")[0] != PROTOCOL:
                self._json(
                    400,
                    _err("protocol_mismatch", f"host speaks SDK protocol {PROTOCOL}, caller asked {asked}"),
                )
                return
            if urlsplit(self.path).path != "/actions/invoke":
                self._json(404, _err("not_found", "not found"))
                return
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {token}"):
                self._json(401, _err("unauthenticated", "missing or invalid actions token"))
                return
            session = holder.session
            if session is None:
                self._json(503, _err("host_starting", "the local host is still starting", retryable=True))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY:
                self._json(413, _err("payload_too_large", "action request is too large"))
                return
            try:
                body = json.loads(self.rfile.read(length))
                if body.get("app"):
                    self._json(
                        400,
                        _err(
                            "cross_app_unsupported",
                            "cross-app invocation requires Tangram OS — "
                            "unsupported in the standalone local host",
                        ),
                    )
                    return
                result = invoke_backend_action(
                    session,
                    body["resource_type"],
                    body["action"],
                    body.get("args", {}),
                )
                self._json(200, {"result": result})
            except PermissionError as error:
                self._json(403, _err("confirmation_required_unattended", str(error)))
            except (KeyError, TypeError, ValueError) as error:
                self._json(400, _err("invalid_request", f"invalid action request: {error}"))
            except Exception as error:  # surfaced verbatim to the app author
                self._json(400, _err("action_failed", str(error)[:2000]))

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Tangram-SDK-Protocol", PROTOCOL)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    return Handler
