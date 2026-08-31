"""User-level installed-app store under `~/.tangram`.

`tangram-app app install` copies a VALIDATED manifest package — a source
directory, a `.tar.gz`/`.zip` archive, or an https URL to one (the App Hub
distributes apps as tarballs) — into `~/.tangram/apps/`. Installed apps are
then addressable by app id (`group/name`, or the bare name when unique)
everywhere the CLI accepts a package path, so an agent can `open` an
installed app's UI or `call` its actions without knowing where it lives.
Override the store root with `TANGRAM_HOME`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

from .app import TangramApp

_ENTRY_FILE = ".install.json"
_IGNORED = (".preview", ".git")


class LocalStoreError(ValueError):
    """A store operation failed for a caller-actionable reason."""


def tangram_home() -> Path:
    override = os.environ.get("TANGRAM_HOME")
    return Path(override) if override else Path.home() / ".tangram"


def apps_root() -> Path:
    return tangram_home() / "apps"


def install_app(source: str, *, force: bool = False) -> dict:
    """Install a manifest package into the store and return its entry."""
    with tempfile.TemporaryDirectory() as scratch:
        package_root = _materialize(source, Path(scratch))
        app = TangramApp.from_package(package_root)
        package = app.graph.package.to_dict()
        app_id = package.get("id") or ""
        if "/" not in app_id:
            raise LocalStoreError(f"package has no usable id: {app_id!r}")
        destination = apps_root() / app_id.replace("/", "__")
        if destination.exists():
            if not force:
                raise FileExistsError(
                    f"{app_id} is already installed at {destination} (use --force to replace)"
                )
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            package_root, destination, ignore=shutil.ignore_patterns(*_IGNORED)
        )
        entry = {
            "id": app_id,
            "version": package.get("version"),
            "root": str(destination),
            "source": source,
            "installedAt": datetime.now(timezone.utc).isoformat(),
        }
        (destination / _ENTRY_FILE).write_text(json.dumps(entry), encoding="utf-8")
        return entry


def list_installed() -> list[dict]:
    root = apps_root()
    if not root.is_dir():
        return []
    entries = []
    for child in sorted(root.iterdir()):
        marker = child / _ENTRY_FILE
        if not marker.is_file():
            continue
        try:
            entry = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            entry["root"] = str(child)
            entries.append(entry)
    return entries


def resolve_installed(ref: str) -> dict | None:
    """Match an installed app by exact id, or by bare name when unique."""
    entries = list_installed()
    exact = [e for e in entries if e["id"] == ref]
    if exact:
        return exact[0]
    named = [e for e in entries if e["id"].rsplit("/", 1)[-1] == ref]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise LocalStoreError(
            f"{ref!r} is ambiguous: " + ", ".join(sorted(e["id"] for e in named))
        )
    return None


def uninstall_app(ref: str) -> dict:
    entry = resolve_installed(ref)
    if entry is None:
        raise LocalStoreError(f"no installed app matches {ref!r}")
    shutil.rmtree(entry["root"])
    return entry


def resolve_target(target: str | Path) -> Path:
    """CLI targets: an existing path wins; otherwise try the store."""
    path = Path(target)
    if path.exists():
        return path
    if isinstance(target, str):
        entry = resolve_installed(target)
        if entry is not None:
            return Path(entry["root"])
    return path


def _materialize(source: str, scratch: Path) -> Path:
    """Turn `source` into an on-disk package root inside/outside scratch."""
    if source.startswith(("http://", "ftp://")):
        raise LocalStoreError("only https:// URLs are allowed")
    if source.startswith("https://"):
        archive = scratch / "download"
        with urllib.request.urlopen(source, timeout=120) as response:
            with archive.open("wb") as sink:
                shutil.copyfileobj(response, sink)
        return _package_root(_extract(archive, scratch / "unpacked", label=source))
    path = Path(source).expanduser()
    if path.is_dir():
        return _package_root(path)
    if path.is_file():
        return _package_root(_extract(path, scratch / "unpacked", label=str(path)))
    raise LocalStoreError(f"install source does not exist: {source}")


def _extract(archive: Path, into: Path, *, label: str) -> Path:
    into.mkdir(parents=True)
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar:
            tar.extractall(into, filter="data")  # refuses traversal/links
        return into
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                member = into / name
                if not member.resolve().is_relative_to(into.resolve()):
                    raise LocalStoreError(f"unsafe archive member {name!r} in {label}")
            bundle.extractall(into)
        return into
    raise LocalStoreError(f"{label} is neither a tar nor a zip archive")


def _package_root(candidate: Path) -> Path:
    """The package root is the directory holding `manifests/` — either the
    candidate itself or exactly one of its children (hub tarball layout)."""
    if (candidate / "manifests").is_dir():
        return candidate
    children = [
        child
        for child in candidate.iterdir()
        if child.is_dir() and (child / "manifests").is_dir()
    ]
    if len(children) == 1:
        return children[0]
    raise LocalStoreError(
        f"{candidate} does not contain a manifest package (no manifests/ directory)"
    )
