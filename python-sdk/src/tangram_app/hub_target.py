"""Credential-free, fail-closed target references for multi-command agent work."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile

from .local_os import ambient_context, base_url, load_os_credential
from .local_store import tangram_home


class HubError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def encode(target: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(target, sort_keys=True).encode()).decode().rstrip("=")


def credential_binding(credential: dict) -> str:
    home = tangram_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".hub-reference-key"
    if not path.exists():
        # Publish only a fully written key; concurrent resolvers use the winner.
        with tempfile.NamedTemporaryFile(dir=home, delete=False) as temp:
            temporary = temp.name
            temp.write(os.urandom(32))
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    key = path.read_bytes()
    if len(key) != 32:
        raise HubError("invalid_target", "invalid local reference key; repair it before resolving targets")
    message = json.dumps([credential.get("instance"), base_url(credential), credential["token"]]).encode()
    return "hmac-sha256:" + hmac.new(key, message, hashlib.sha256).hexdigest()


def resolve(args) -> dict:
    from .cli_targets import resolve_os_selector

    selectors = [getattr(args, name, None) for name in ("workspace", "instance")]
    if args.local and any(value is not None for value in selectors):
        raise HubError("invalid_target", "--local conflicts with workspace/instance selectors")
    if args.local:
        context = {"instance": None}
    elif any(value is not None for value in selectors):
        context = resolve_os_selector(args)
    else:
        context = ambient_context()
    if not context.get("instance"):
        if context.get("workspace"):
            raise HubError("invalid_target", "a workspace requires a Tangram OS instance")
        target = {"version": 1, "lane": "local", "store": str(tangram_home().resolve())}
    else:
        if not context.get("workspace"):
            raise HubError("invalid_target", "select a complete INSTANCE/WORKSPACE target")
        credential = load_os_credential(context["instance"], expected_url=context.get("profile_url"))
        target = {
            "version": 1, "lane": "workspace", "instance": context["instance"],
            "workspace": context["workspace"], "endpoint": base_url(credential),
            # A credential changes on re-login: resolve a new reference then.
            # No bearer token or reversible encoding of one leaves this process.
            "credentialBinding": credential_binding(credential),
        }
    return {"target": target, "targetRef": encode(target)}


def load(reference: str) -> tuple[dict, dict | None]:
    try:
        if len(reference) > 8192:
            raise ValueError()
        target = json.loads(base64.b64decode(reference + "=" * (-len(reference) % 4), altchars=b"-_", validate=True))
        if not isinstance(target, dict) or target.get("version") != 1:
            raise ValueError()
        if target.get("lane") == "local":
            if set(target) != {"version", "lane", "store"} or target["store"] != str(tangram_home().resolve()):
                raise ValueError()
            return target, None
        required = {"version", "lane", "instance", "workspace", "endpoint", "credentialBinding"}
        if set(target) != required or target["lane"] != "workspace":
            raise ValueError()
        if any(not isinstance(target[key], str) or not target[key] for key in required - {"version"}):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HubError("invalid_target", "invalid target reference; resolve the intended target again") from None
    credential = load_os_credential(target["instance"])
    if base_url(credential) != target["endpoint"] or not hmac.compare_digest(credential_binding(credential), target["credentialBinding"]):
        raise HubError("target_changed", "endpoint or credential changed; resolve the intended target again")
    return target, credential
