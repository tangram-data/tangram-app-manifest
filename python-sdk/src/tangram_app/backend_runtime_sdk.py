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
# Durable platform schedules (tangram.schedules, composable-app-sdk §5.5) are
# a platform feature: the OS scheduler fires the app's own actions under the
# approved declare_backend_scheduling capability.
schedules = _UnsupportedFacade("schedules", " — durable schedules require Tangram OS (declare_backend_scheduling)")
def _deliver_desktop(title, body):
    """Show one native desktop notification on this machine, or raise.

    Strings travel as argv/env — never interpolated into the scripts — and
    always land after an option terminator (`-` script-from-stdin marker,
    `--`) so app-controlled content can inject neither script nor options."""
    import subprocess
    import sys

    if sys.platform == "darwin":
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run\n"
        )
        subprocess.run(
            ["osascript", "-", title, body],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=True,
        )
        return
    if sys.platform == "win32":
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$x = $t.GetElementsByTagName('text'); "
            "$x.Item(0).AppendChild($t.CreateTextNode($env:TANGRAM_NOTIFY_TITLE)) | Out-Null; "
            "$x.Item(1).AppendChild($t.CreateTextNode($env:TANGRAM_NOTIFY_BODY)) | Out-Null; "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'Tangram App').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        environment = dict(os.environ, TANGRAM_NOTIFY_TITLE=title, TANGRAM_NOTIFY_BODY=body)
    else:
        command = ["notify-send", "--", title, body]
        environment = None
    subprocess.run(command, env=environment, capture_output=True, timeout=10, check=True)


class _Notifications:
    """Developer desktop notifications for the standalone host.

    Platform parity in SHAPE, not in routing: send() shows one native OS
    notification on this machine (macOS/Windows/Linux) — member resolution
    and the email/Slack channels live in Tangram OS, so explicit
    channel="email"/"slack" answers every recipient skipped "unreachable"
    and never falls back. Recipients are echoed queued without resolution;
    list() serves the in-process per-recipient record; the dedupe_key
    window is the process lifetime."""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self._records = []
        self._dedupe = {}
        self._counter = 0

    @staticmethod
    def _copy_envelope(envelope, **extra):
        copy = {
            "id": envelope["id"],
            "queued": list(envelope["queued"]),
            "skipped": [dict(item) for item in envelope["skipped"]],
        }
        copy.update(extra)
        return copy

    def send(self, to, subject, body, link=None, channel="auto", dedupe_key=None):
        if not isinstance(to, (list, tuple)):
            raise ActionError("invalid_request", "to must be a non-empty list of member account ids")
        recipients = list(to)
        if not recipients or not all(isinstance(item, str) and item for item in recipients):
            raise ActionError("invalid_request", "to must be a non-empty list of member account ids")
        for item in recipients:
            if "@" in item:
                raise ActionError(
                    "invalid_request",
                    "address recipients by member account id, never an email/Slack address",
                )
        if not (isinstance(subject, str) and subject and isinstance(body, str) and body):
            raise ActionError("invalid_request", "subject and body must be non-empty strings")
        if channel not in ("auto", "email", "slack"):
            raise ActionError("invalid_request", f"unknown channel {channel!r}")

        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps([recipients, subject, body, link, channel]).encode("utf-8")
        ).hexdigest()
        # One lock over check-deliver-record: a concurrent same-key send must
        # never double-deliver (local mirror of the platform's at-most-once).
        with self._lock:
            if dedupe_key is not None and dedupe_key in self._dedupe:
                seen_digest, envelope = self._dedupe[dedupe_key]
                if seen_digest != digest:
                    raise ActionError(
                        "invalid_request", "dedupe_key was already used with different content"
                    )
                return self._copy_envelope(envelope, deduped=True)
            self._counter += 1
            identifier = f"local-{self._counter}"

            if channel in ("email", "slack"):
                status, queued, skipped = "skipped", [], [
                    {"id": item, "reason": "unreachable"} for item in recipients
                ]
            else:
                title = f"{os.environ.get('TANGRAM_APP', 'local')}: {subject}"
                text = body if link is None else f"{body}\n{link}"
                try:
                    _deliver_desktop(title, text)
                    status = "sent"
                except Exception:
                    status = "failed"
                queued, skipped = recipients, []

            envelope = {"id": identifier, "queued": queued, "skipped": skipped}
            if dedupe_key is not None:
                self._dedupe[dedupe_key] = (digest, envelope)
            for item in recipients:
                self._records.append(
                    {
                        "id": identifier,
                        "account_id": item,
                        "channel": channel,
                        "status": status,
                        "subject": subject,
                    }
                )
            return self._copy_envelope(envelope)

    def list(self, limit=20):
        with self._lock:
            return [dict(row) for row in reversed(self._records)][: max(0, int(limit))]


notifications = _Notifications()
