"""Request a native workspace prompt. No decision or signing authority lives here."""
import json
import math
import urllib.parse
import urllib.request
from .hub_target import HubError
from .local_store import tangram_home


def origin(endpoint):
    """Canonical transport origin. Resolve localhost by construction, never DNS."""
    p = urllib.parse.urlsplit(endpoint)
    try:
        port = p.port
    except ValueError:
        raise HubError("invalid_target", "Invalid approval port") from None
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/") or not p.hostname:
        raise HubError("invalid_target", "Approval requires an origin without credentials, path, query or fragment")
    host = p.hostname
    if p.scheme == "http":
        if host == "localhost": host = "127.0.0.1"
        if host not in ("127.0.0.1", "::1") or port is None:
            raise HubError("approver_unavailable", "Remote workspace approval requires HTTPS; local HTTP requires a loopback address and explicit port")
    elif p.scheme != "https" or host.endswith("."):
        raise HubError("approver_unavailable", "Unsupported approval origin")
    if host == "::1": host = "[::1]"
    suffix = f":{port}" if port is not None and not (p.scheme == "https" and port == 443) else ""
    return f"{p.scheme}://{host}{suffix}"


def request(target, credential, *, invocation_id=None, timeout=300, setup=False):
    if not math.isfinite(timeout) or timeout < 1:
        raise HubError("approver_unavailable", "No time remains for native approval; resume this invocation")
    remote_origin = origin(target["endpoint"])
    try:
        record = json.loads((tangram_home() / "runtime.json").read_text())
        port, token = record["port"], record["token"]
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
            raise ValueError("invalid desktop discovery")
        body = {"mode": "workspace-setup" if setup else ("workspace-approve" if invocation_id else "workspace-enroll"),
                "origin": remote_origin, "workspace": target["workspace"],
                # Only the existing CLI bearer credential; never a refresh/enrollment secret.
                "token": credential["token"], "timeoutSeconds": min(300, int(timeout))}
        if invocation_id: body["invocationId"] = invocation_id
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/local/v1/workspace-approvals",
            data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=min(300, timeout) + 10) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024: raise ValueError("oversized approval response")
        result = json.loads(raw)
        if not isinstance(result, dict) or (invocation_id and result.get("invocationId") != invocation_id):
            raise ValueError("native approval returned another invocation")
        return result
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Do not interpolate transport exceptions: they may contain credentials.
        raise HubError("approver_unavailable", "Native approval did not complete. Open a provisioned Tangram Desktop release and resume the same invocation.") from error
