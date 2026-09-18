"""Install a manifest package into a Tangram OS environment.

The OS lane of `tangram-app app install --workspace ...`: the same
governed `apps:install` endpoint and wire shape the native CLI speaks
(`{"source": {"kind": "local-package", "files": {...}}, "upgrade",
"dryRun"}` under a Bearer token), reusing the native CLI's stored
credentials in `~/.tangram/.credentials` (+ `.HEAD` for the current
instance) so `tangram use <instance>` also selects the SDK's target.
The user-level store lane (`local_store.py`) is untouched — this file is
for deploying INTO a running Tangram OS, local (`localhost:8081`) or
remote.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .local_store import tangram_home

# Mirrors the native CLI's package-tree contract (AppPackages.readTree).
_MAX_TREE_FILES = 4096
_MAX_TREE_BYTES = 16 * 1024 * 1024
_WORKING_COPY_NOISE = frozenset(("AGENTS.md", "CLAUDE.md", "README.md"))


class LocalOsError(ValueError):
    """An OS-install operation failed for a caller-actionable reason."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never re-send the bearer token to a redirected host


def _canonical_endpoint(url: str) -> tuple[bool, str, int]:
    """Mirror the native CLI's HostUrl.parse: the store may hold a bare host
    (host = https:443, localhost/127.0.0.1[:port] = the http quickstart
    runtime, default 8081) or a full URL (desktop-written). Hostnames compare
    case-insensitively; http loopback spellings alias."""
    trimmed = url.strip().rstrip("/").lower()
    if trimmed.startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(trimmed)
        tls = parts.scheme == "https"
        host = parts.hostname or trimmed
        port = parts.port or (443 if tls else 80)
    else:
        host, sep, maybe_port = trimmed.partition(":")
        explicit = int(maybe_port) if sep and maybe_port.isdigit() else None
        if host in ("localhost", "127.0.0.1"):
            tls, port = False, explicit or 8081
        else:
            tls, port = True, explicit or 443
    if not tls and host == "127.0.0.1":
        host = "localhost"
    return (tls, host, port)


def _same_endpoint(a: str, b: str) -> bool:
    return _canonical_endpoint(a) == _canonical_endpoint(b)


def load_os_credential(instance: str | None = None, *, expected_url: str | None = None) -> dict:
    """The native CLI's stored credential for `instance` (default: `.HEAD`),
    matched by name EXACTLY and validated against the profile's endpoint
    (`expected_url` when the caller already resolved it, else `.HEAD` /
    `.contexts`) — a credential issued for another URL must not silently
    redirect execution (native-CLI parity)."""
    home = tangram_home()
    try:
        credentials = json.loads((home / ".credentials").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LocalOsError(
            f"no Tangram OS credentials at {home / '.credentials'} — log in with "
            "the native CLI first (`tangram use <instance>`)"
        ) from error
    if instance is None:
        try:
            head = json.loads((home / ".HEAD").read_text(encoding="utf-8"))
            instance = head["instance"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LocalOsError(
                "no current instance — pass --instance or run `tangram use <instance>`"
            ) from error
        if expected_url is None:
            head_url = head.get("url") if isinstance(head, dict) else None
            expected_url = head_url if isinstance(head_url, str) and head_url else None
    elif expected_url is None:
        expected_url = _profile_url(home, instance)
    rows = [entry for entry in (credentials if isinstance(credentials, list) else []) if isinstance(entry, dict)]
    for entry in rows:
        if entry.get("instance") == instance and entry.get("token"):
            # Endpoint identity, not string identity: the store holds bare
            # hosts (CLI) and full URLs (desktop) for the same server.
            if expected_url and not _same_endpoint(str(entry.get("url") or ""), expected_url):
                raise LocalOsError(
                    f"stored credential for {instance!r} was issued for {entry.get('url')!r} but the "
                    f"instance profile points at {expected_url!r} — log in again with the native CLI"
                )
            return entry
    raise LocalOsError(
        f"no stored credential for instance {instance!r} — log in with the native CLI"
    )


def _profile_url(home: Path, instance: str) -> str | None:
    """The instance's endpoint from the native CLI's `.contexts` profile
    (None when the file or entry is absent; corrupt fails explicitly)."""
    path = home / ".contexts"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LocalOsError(f"cannot read {path}: {error}") from None
    if not text.strip():
        return None
    try:
        rows = json.loads(text)
    except ValueError as error:
        raise LocalOsError(f"{path} is corrupt ({error}) — fix or remove it") from None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("instance") == instance:
            url = row.get("url")
            return url if isinstance(url, str) and url else None
    return None


def ambient_context(*, ignore_workspace: bool = False) -> dict:
    """Instance + workspace (+ `.HEAD`'s profile_url) of the ambient
    context. Precedence per axis: `TANGRAM_INSTANCE`/`TANGRAM_WORKSPACE` env
    (per-shell/per-agent pinning, the kubectl `--context` lesson) over the
    persisted `.HEAD` — read ONCE. An env instance NEVER pairs with a
    `.HEAD` workspace: that workspace belongs to the persisted instance,
    and carrying it across is the exact cross-instance hazard the model
    forbids. `ignore_workspace` selects an explicit instance-wide scope;
    workspace defaults have no meaning in that scope."""
    env_instance = os.environ.get("TANGRAM_INSTANCE") or None
    env_workspace = None if ignore_workspace else os.environ.get("TANGRAM_WORKSPACE") or None
    if env_instance in ("local", "machine"):
        # The machine-context words pin THE MACHINE, exactly like
        # `tangram use local` — they never name an OS instance.
        if env_workspace:
            raise LocalOsError(
                f"TANGRAM_INSTANCE={env_instance} pins the machine context, which has no "
                "workspaces — unset TANGRAM_WORKSPACE"
            )
        return {"instance": None, "workspace": None, "profile_url": None}
    if env_instance:
        return {"instance": env_instance, "workspace": env_workspace, "profile_url": None}
    head_path = tangram_home() / ".HEAD"
    try:
        text = head_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as error:
        raise LocalOsError(f"cannot read {head_path}: {error}") from None
    if not text.strip():
        return {"instance": None, "workspace": env_workspace, "profile_url": None}
    # MISSING/empty means the machine context; a CORRUPT or unexpectedly
    # shaped file must fail explicitly, never silently read as a different
    # target (the native CLI's decoder rejects the same shapes).
    try:
        head = json.loads(text)
    except ValueError as error:
        raise LocalOsError(f"{head_path} is corrupt ({error}) — fix or remove it") from None
    instance = head.get("instance") if isinstance(head, dict) else None
    workspace = head.get("workspace") if isinstance(head, dict) else None
    if (
        not isinstance(head, dict)
        or "instance" not in head
        or not isinstance(instance, str)
        or not isinstance(head.get("url", ""), str)
        or not isinstance(workspace, (str, type(None)))
    ):
        raise LocalOsError(f"{head_path} is corrupt (unexpected context shape) — fix or remove it")
    url = head.get("url")
    return {
        "instance": instance or None,
        "workspace": None if ignore_workspace else env_workspace or workspace or None,
        "profile_url": url if isinstance(url, str) and url else None,
    }


def env_workspace() -> str | None:
    """The per-session workspace pin alone (no `.HEAD` involvement)."""
    return os.environ.get("TANGRAM_WORKSPACE") or None


def configured_instances() -> list[str]:
    """Names of all stored instance credentials (empty when none)."""
    try:
        credentials = json.loads((tangram_home() / ".credentials").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(
        entry["instance"]
        for entry in (credentials if isinstance(credentials, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("instance"), str)
    )


def base_url(credential: dict) -> str:
    """CliHttp parity via the canonical endpoint parser — handles every
    stored spelling (bare host, host:port, localhost[:port], full URL)."""
    tls, host, port = _canonical_endpoint(str(credential.get("url") or ""))
    return f"{'https' if tls else 'http'}://{host}:{port}"


def read_tree(root: str | Path) -> dict[str, str]:
    """The package tree exactly as the native CLI ships it: UTF-8 text files,
    no dot-paths, no symlinks, top-level working-copy noise dropped."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise LocalOsError(f"{base} is not a directory")
    files: dict[str, str] = {}
    total = 0
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        parts = relative.parts
        if any(part.startswith(".") for part in parts):
            continue
        if len(parts) == 1 and parts[0] in _WORKING_COPY_NOISE:
            continue
        if path.is_symlink():
            raise LocalOsError(
                f"refusing symlink '{relative}' — a package tree contains plain files only"
            )
        if not path.is_file():
            continue
        data = path.read_bytes()
        total += len(data)
        if len(files) >= _MAX_TREE_FILES:
            raise LocalOsError(f"{base} holds more than {_MAX_TREE_FILES} files")
        if total > _MAX_TREE_BYTES:
            raise LocalOsError(f"{base} exceeds {_MAX_TREE_BYTES} bytes of package content")
        try:
            files["/".join(parts)] = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LocalOsError(
                f"'{relative}' is not UTF-8 text — package trees ship text files only"
            ) from error
    if not files:
        raise LocalOsError(f"{base} contains no package files")
    return files


def os_request(
    credential: dict,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout_seconds: float = 60.0,
    label: str = "OS request",
) -> dict:
    """One authenticated core-server call: Bearer token, no proxy, no
    redirects (never re-send credentials to another location)."""
    target = f"{base_url(credential)}{path}"
    headers = {"Authorization": f"Bearer {credential['token']}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(target, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise LocalOsError(
                f"{label} answered a redirect ({error.code}); refusing to re-send "
                "credentials to another location"
            ) from None
        detail = error.read().decode("utf-8", "replace")[:1000]
        raise LocalOsError(f"{label} answered {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise LocalOsError(f"could not reach Tangram OS at {target}: {error.reason}") from None


def os_install(
    package_root: str | Path,
    workspace: str,
    *,
    credential: dict | None = None,
    instance: str | None = None,
    token: str | None = None,
    url: str | None = None,
    dry_run: bool = False,
    upgrade: bool = False,
    timeout_seconds: float = 300.0,
) -> dict:
    """POST the package to `{base}/api/core/v1/workspaces/{ws}/apps:install`.

    A preloaded `credential` is used verbatim so the target the caller
    described (and the user consented to) is the one that executes."""
    if credential is None:
        if token and url:
            credential = {"token": token, "url": url}
        elif token or url:
            raise LocalOsError("--token and --os-url go together (or use a stored instance)")
        else:
            credential = load_os_credential(instance)
    files = read_tree(package_root)
    body = {
        "source": {"kind": "local-package", "files": files},
        "upgrade": upgrade,
        "dryRun": dry_run,
    }
    path = f"/api/core/v1/workspaces/{urllib.parse.quote(workspace, safe='')}/apps:install"
    result = os_request(
        credential, "POST", path, body, timeout_seconds=timeout_seconds, label="OS install"
    )
    return {
        "workspace": workspace,
        "target": base_url(credential),
        "dryRun": dry_run,
        "result": result,
    }
