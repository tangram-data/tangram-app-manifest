"""Blocking desktop invocation transport; never silently falls back."""
import errno
import json
import urllib.request
import urllib.error
from .hub_target import HubError
from .local_store import tangram_home


class DesktopNotRunningError(HubError):
    """This attempt did not send a request; only ungated reads may fall back."""

    def __init__(self, message):
        super().__init__("approver_unavailable", message)


def call(args, arguments):
    path = tangram_home() / "runtime.json"
    if not path.is_file():
        raise DesktopNotRunningError("Open Tangram Desktop to approve this action, then resume this invocation.")
    attempted = False
    try:
        record = json.loads(path.read_text())
        port, token, generation = record["port"], record["token"], record["generation"]
        if type(port) is not int or not 1 <= port <= 65535 or not token or not generation:
            raise ValueError("invalid discovery record")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/local/v1/app-invocations",
            data=json.dumps({"invocationId": args.invocation_id, "action": args.action,
                             "contract": args.contract, "input": arguments,
                             "generation": generation, "timeoutSeconds": min(int(args.timeout), 3600)}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        # No proxies or redirects: never send the local token elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *unused, **kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        attempted = True
        with opener.open(request, timeout=args.timeout + 15) as response:
            result = json.load(response)
        if result.get("invocationId") != args.invocation_id:
            raise ValueError("supervisor returned another invocation")
        return result
    except (OSError, ValueError, KeyError, TypeError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if attempted and isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
            raise DesktopNotRunningError(
                "Desktop connection was refused; this request was not sent. "
                "Open Tangram Desktop and resume this invocation.") from error
        if not attempted:
            raise HubError("approver_unavailable", f"Desktop invocation was not sent: {error}. Open or repair Tangram Desktop, then resume this invocation.") from error
        raise HubError("supervisor_unavailable", f"Desktop invocation did not complete: {error}. "
                       f"Do not create another invocation ID; retry {args.invocation_id} to reconcile its result.") from error


def status(args):
    """Return a desktop record, or None when the ID belongs to standalone."""
    path = tangram_home() / "runtime.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
        port = record["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid desktop port")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/local/v1/app-invocations/{args.invocation_id}",
            headers={"Authorization": f"Bearer {record['token']}"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *unused, **kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=args.timeout) as response:
            result = json.load(response)
        if result.get("error", {}).get("code") == "invocation_not_found": return None
        return result
    except (OSError, ValueError, KeyError) as error:
        raise HubError("supervisor_unavailable", f"Cannot reconcile desktop invocation: {error}") from error
