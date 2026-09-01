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


def load_os_credential(instance: str | None = None) -> dict:
    """The native CLI's stored credential for `instance` (default: `.HEAD`)."""
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
            instance = json.loads((home / ".HEAD").read_text(encoding="utf-8"))["instance"]
        except (OSError, ValueError, KeyError) as error:
            raise LocalOsError(
                "no current instance — pass --instance or run `tangram use <instance>`"
            ) from error
    for entry in credentials if isinstance(credentials, list) else []:
        if isinstance(entry, dict) and entry.get("instance") == instance and entry.get("token"):
            return entry
    raise LocalOsError(
        f"no stored credential for instance {instance!r} — log in with the native CLI"
    )


def base_url(credential: dict) -> str:
    """CliHttp parity: bare `localhost` is the local http instance on 8081."""
    url = credential.get("url") or ""
    if url == "localhost":
        return "http://localhost:8081"
    if url.startswith(("http://", "https://")):
        return url.rstrip("/")
    return f"https://{url}:443"


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


def os_install(
    package_root: str | Path,
    workspace: str,
    *,
    instance: str | None = None,
    token: str | None = None,
    url: str | None = None,
    dry_run: bool = False,
    upgrade: bool = False,
    timeout_seconds: float = 300.0,
) -> dict:
    """POST the package to `{base}/api/core/v1/workspaces/{ws}/apps:install`."""
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
    target = (
        f"{base_url(credential)}/api/core/v1/workspaces/"
        f"{urllib.parse.quote(workspace, safe='')}/apps:install"
    )
    request = urllib.request.Request(
        target,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credential['token']}",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise LocalOsError(
                f"OS install answered a redirect ({error.code}); refusing to re-send "
                "credentials to another location"
            ) from None
        detail = error.read().decode("utf-8", "replace")[:1000]
        raise LocalOsError(f"OS install answered {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise LocalOsError(f"could not reach Tangram OS at {target}: {error.reason}") from None
    return {
        "workspace": workspace,
        "target": base_url(credential),
        "dryRun": dry_run,
        "result": result,
    }
