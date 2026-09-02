"""Environment diagnosis and self-healing for the standalone SDK.

`tangram-app doctor` reports every prerequisite with an actionable hint;
`--fix` auto-installs what can be installed safely without sudo — today
the Pkl CLI, a single static binary fetched into `~/.tangram/bin/` (the
evaluator falls back to that location when `pkl` is not on PATH, so no
shell configuration is needed). PostgreSQL stays a diagnosed-not-installed
prerequisite: it needs the platform package manager.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request

from .local_store import tangram_home

PKL_VERSION = "0.25.3"
_PKL_ASSETS = {
    ("Darwin", "arm64"): "pkl-macos-aarch64",
    ("Darwin", "x86_64"): "pkl-macos-amd64",
    ("Linux", "x86_64"): "pkl-linux-amd64",
    ("Linux", "aarch64"): "pkl-linux-aarch64",
}


class DoctorError(ValueError):
    """A --fix step failed for a caller-actionable reason."""


def managed_pkl_path() -> Path:
    return tangram_home() / "bin" / ("pkl.exe" if os.name == "nt" else "pkl")


def find_pkl() -> str | None:
    """PATH first, then the doctor-managed copy."""
    on_path = shutil.which("pkl")
    if on_path:
        return on_path
    managed = managed_pkl_path()
    return str(managed) if managed.is_file() and os.access(managed, os.X_OK) else None


def _postgres_hint() -> str:
    if platform.system() == "Darwin":
        return "brew install postgresql@16 (then keep its bin on PATH)"
    return "apt install postgresql (Debian/Ubuntu) or your distro's package"


def diagnose() -> dict:
    """All prerequisite checks with per-item hints; `ok` = required ones pass."""
    checks = []

    python_ok = sys.version_info >= (3, 11)
    checks.append(
        {
            "name": "python",
            "ok": python_ok,
            "required": True,
            "detail": platform.python_version(),
            "hint": None if python_ok else "install Python 3.11+ (3.12+ to run app backends)",
        }
    )
    # Mirror the runtime's own interpreter resolution (TANGRAM_LOCAL_PYTHON,
    # python3.12/python3, well-known paths) so the verdict matches `run`.
    from .local_runtime import _resolve_python, _verify_python

    try:
        backend_python = _resolve_python(None)
        _verify_python(backend_python)
        backend_detail: str | None = str(backend_python)
        backend_ok = True
    except Exception as error:
        backend_detail = str(error)
        backend_ok = False
    checks.append(
        {
            "name": "python-3.12-backends",
            "ok": backend_ok,
            "required": False,
            "detail": backend_detail,
            "hint": None
            if backend_ok
            else "install Python 3.12+ (or set TANGRAM_LOCAL_PYTHON) for `run`/`open`/`call --local`",
        }
    )

    pkl = find_pkl()
    checks.append(
        {
            "name": "pkl",
            "ok": pkl is not None,
            "required": True,
            "detail": pkl,
            "hint": None
            if pkl
            else "run `tangram-app doctor --fix` (installs it), or install from pkl-lang.org",
        }
    )

    postgres = shutil.which("initdb") is not None and shutil.which("pg_ctl") is not None
    checks.append(
        {
            "name": "postgresql",
            "ok": postgres,
            "required": False,
            "detail": "needed only for apps declaring a database claim",
            "hint": None if postgres else _postgres_hint(),
        }
    )

    node = shutil.which("npm") is not None or shutil.which("node") is not None
    checks.append(
        {
            "name": "node",
            "ok": node,
            "required": False,
            "detail": "needed only for sandboxed React UI components",
            "hint": None if node else "install Node.js (e.g. brew install node)",
        }
    )

    native = shutil.which("tangram") is not None
    checks.append(
        {
            "name": "tangram-native-cli",
            "ok": native,
            "required": False,
            "detail": "publishing conformance authority + Tangram OS tooling",
            "hint": None if native else "see TANGRAM_CLI.md in tangram-app-manifest",
        }
    )

    return {
        "ok": all(check["ok"] for check in checks if check["required"]),
        "checks": checks,
    }


def install_pkl(*, version: str = PKL_VERSION) -> Path:
    """Fetch the platform's static Pkl binary into `~/.tangram/bin/`."""
    key = (platform.system(), platform.machine())
    asset = _PKL_ASSETS.get(key)
    if asset is None:
        raise DoctorError(
            f"no known Pkl binary for {key[0]}/{key[1]} — install it manually from pkl-lang.org"
        )
    url = f"https://github.com/apple/pkl/releases/download/{version}/{asset}"
    target = managed_pkl_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=300) as response:
        with scratch.open("wb") as sink:
            shutil.copyfileobj(response, sink)
    scratch.chmod(scratch.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Verify the DOWNLOAD before it replaces anything: a broken fetch must
    # never clobber a working managed binary.
    try:
        completed = subprocess.run(
            [str(scratch), "--version"], capture_output=True, text=True, timeout=60
        )
    except OSError as error:
        scratch.unlink(missing_ok=True)
        raise DoctorError(f"downloaded Pkl binary does not execute: {error}") from None
    if completed.returncode != 0 or "Pkl" not in completed.stdout:
        scratch.unlink(missing_ok=True)
        raise DoctorError(
            f"downloaded Pkl binary failed --version (exit {completed.returncode})"
        )
    os.replace(scratch, target)
    return target


def fix() -> list[dict]:
    """Apply every safe automatic fix; returns what was done."""
    applied = []
    if find_pkl() is None:
        installed = install_pkl()
        applied.append({"check": "pkl", "action": f"installed {PKL_VERSION} at {installed}"})
    return applied
