"""Staged as ``tangram`` inside locally hosted Python app backends.

Keep the database and context contract aligned with Tangram Desktop's vendored
backend SDK. Platform-facade features fail explicitly until the standalone
host implements their local providers.
"""

from __future__ import annotations

import os

# Kept in lockstep with the SDK package version that stages this module.
__version__ = "0.1.0.dev0"
# SDK<->host wire protocol MAJOR (docs/sdk-host-abi.md). Hosts that answer
# with a different major are refused loudly at the first call.
PROTOCOL = "1"


class ActionError(RuntimeError):
    """A host-reported action failure with a stable machine-readable code."""

    def __init__(self, code, message, retryable=False):
        super().__init__(f"action failed [{code}]: {message}")
        self.code = code
        self.retryable = retryable


def _normalize_error_envelope(detail):
    """Normalize the error envelope: canonical {"error": {code, message,
    retryable}} or the protocol-1 legacy {"error": "<text>"} string form.
    Only correctly typed fields are honored — anything else degrades to the
    safe defaults instead of leaking through."""
    if isinstance(detail, dict):
        inner = detail.get("error", detail)
        if isinstance(inner, dict):
            code = inner.get("code")
            message = inner.get("message")
            retryable = inner.get("retryable")
            return (
                code if isinstance(code, str) and code else "action_failed",
                message if isinstance(message, str) and message else "unknown error",
                retryable if isinstance(retryable, bool) else False,
            )
        if isinstance(inner, str):
            return ("action_failed", inner, False)
    return ("action_failed", str(detail), False)


_HEADER_CONTEXT = {
    "x-tangram-workspace": "workspace",
    "x-tangram-actor": "actor",
    "x-tangram-action": "action",
    "x-tangram-effect": "effect",
    "x-tangram-invocation-id": "invocationId",
}


def context(request=None):
    """Return the current local invocation context."""
    value = {
        "workspace": os.environ.get("TANGRAM_WORKSPACE", "local"),
        "app": os.environ.get("TANGRAM_APP", "local"),
    }
    if request is None:
        return value
    headers = getattr(request, "headers", None) or {}
    injected = False
    for header, key in _HEADER_CONTEXT.items():
        header_value = headers.get(header)
        if header_value:
            value[key] = header_value
            injected = True
    if not injected:
        value["actor"] = "anonymous"
    return value


class _Db:
    def __init__(self):
        self._pool = None

    def _get_pool(self):
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            conninfo = (
                f"host={os.environ['TANGRAM_DB_HOST']} "
                f"port={os.environ['TANGRAM_DB_PORT']} "
                f"dbname={os.environ['TANGRAM_DB_NAME']} "
                f"user={os.environ['TANGRAM_DB_USER']} "
                f"password={os.environ['TANGRAM_DB_PASSWORD']}"
            )
            self._pool = ConnectionPool(
                conninfo,
                min_size=0,
                max_size=int(os.environ.get("TANGRAM_DB_POOL_SIZE", "4")),
                kwargs={"prepare_threshold": None},
            )
        return self._pool

    def query(self, sql, params=None):
        """Run SQL that returns rows and return them as dictionaries."""
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params or {})
                columns = [item.name for item in cursor.description] if cursor.description else []
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def execute(self, sql, params=None):
        """Run one data-changing statement and return its affected-row count."""
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params or {})
                return cursor.rowcount


class _UnsupportedFacade:
    def __init__(self, capability, hint=""):
        self._capability = capability
        self._hint = hint

    def __getattr__(self, operation):
        def unavailable(*_args, **_kwargs):
            raise RuntimeError(
                f"tangram.{self._capability}.{operation} is not available in the standalone local host"
                + self._hint
            )

        return unavailable


class _Actions:
    """Compose the app's OWN governed actions through the local host pipeline
    (platform parity: unattended — irreversible / confirmation-gated actions
    refuse; cross-app requires Tangram OS)."""

    def invoke(self, resource_type, action, args=None, app=None):
        if app:
            raise RuntimeError(
                "tangram.actions.invoke(app=...) requires Tangram OS — cross-app "
                "invocation is unsupported in the standalone local host"
            )
        base = os.environ.get("TANGRAM_LOCAL_ACTIONS_URL")
        if not base:
            raise RuntimeError(
                "tangram.actions.invoke is not available: the local host did not "
                "expose an actions endpoint (upgrade the tangram-app SDK)"
            )
        import json
        import urllib.error
        import urllib.request

        import time

        payload = json.dumps(
            {"resource_type": resource_type, "action": action, "args": args or {}}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Tangram-SDK-Protocol": PROTOCOL}
        token = os.environ.get("TANGRAM_LOCAL_ACTIONS_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # The host attaches the session only after the backend answers its
        # readiness probe; a startup hook calling here retries 503 briefly
        # instead of failing deterministically.
        deadline = time.monotonic() + 30
        while True:
            request = urllib.request.Request(
                f"{base}/actions/invoke", data=payload, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    answered = response.headers.get("X-Tangram-SDK-Protocol")
                    if answered and answered.split(".")[0] != PROTOCOL:
                        raise ActionError(
                            "protocol_mismatch",
                            f"host speaks SDK protocol {answered}, this module speaks {PROTOCOL}",
                        )
                    return json.loads(response.read())["result"]
            except urllib.error.HTTPError as error:
                answered = (error.headers or {}).get("X-Tangram-SDK-Protocol")
                if answered and answered.split(".")[0] != PROTOCOL:
                    raise ActionError(
                        "protocol_mismatch",
                        f"host speaks SDK protocol {answered}, this module speaks {PROTOCOL}",
                    ) from None
                if error.code == 503 and time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
                raw = error.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = raw
                code, message, retryable = _normalize_error_envelope(parsed)
                raise ActionError(code, message, retryable) from None


db = _Db()
storage = _UnsupportedFacade("storage")
secrets = _UnsupportedFacade("secrets")
actions = _Actions()
# Declared workspace queries (tangram.sql.run) are a platform feature: they
# need approved statements, workspace engines and the SQL proxy.
sql = _UnsupportedFacade("sql", " — declared workspace queries require Tangram OS")
