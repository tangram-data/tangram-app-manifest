"""`tangram` — the pip-side front door.

The consolidation plan makes the native Scala CLI the primary `tangram`
front door, delegating the agent lane to `tangram-app`. This module is
the MIRROR for machines that only have the pip SDK: it exposes a
`tangram` entrypoint that runs agent-lane commands in-process and
forwards everything else (platform/OS operations) to the native binary
when one exists on PATH — excluding itself, so PATH shadowing is
harmless whichever artifact resolves first. Without a native binary,
platform commands fail with the install pointer instead of a mystery.

The routing table deliberately matches the native FrontDoor's:
identity passthrough for the agent lane, machine-context `app` verbs
only when no workspace/instance selector is present.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

_PASSTHROUGH = frozenset(
    (
        "doctor", "actions", "call", "connect", "disconnect",
        "validate", "build", "inspect", "run", "open", "skill",
    )
)
_MACHINE_APP_VERBS = frozenset(("list", "ls", "get", "install", "uninstall", "open"))
_SELECTORS = ("--workspace", "-w", "--instance", "--os-url")
_NATIVE_HINT = (
    "this is a Tangram OS / platform command; it needs the native Tangram CLI — "
    "install it from TANGRAM_CLI.md at github.com/tangram-data/tangram-app-manifest"
)


def route(args: list[str]) -> list[str] | None:
    """The `tangram-app` argv to run in-process, or None → native lane."""
    if not args:
        return None
    if args[0] in _PASSTHROUGH:
        return args
    if len(args) >= 2 and args[0] == "app" and args[1] in _MACHINE_APP_VERBS:
        rest = args[2:]
        if any(_is_selector(token) for token in rest):
            return None
        if args[1] == "ls":
            return ["app", "list", *rest]
        if args[1] == "open":
            return ["open", *rest]
        return args
    return None


def _is_selector(token: str) -> bool:
    return token in _SELECTORS or any(token.startswith(s + "=") for s in _SELECTORS)


def find_native(argv0: str | None = None) -> str | None:
    """The native `tangram` binary on PATH, excluding this entrypoint."""
    own = Path(argv0 or sys.argv[0]).resolve()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "tangram"
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            resolved = candidate.resolve()
            if resolved == own:
                continue
            # Skip other copies of THIS shim (console scripts import this
            # module); forward only to a real (non-python-entrypoint) binary.
            head = resolved.read_bytes()[:512]
            if b"tangram_app" in head:
                continue
            return str(candidate)
        except OSError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    delegated = route(args)
    if delegated is not None:
        from .cli import main as cli_main

        return cli_main(delegated)
    native = find_native()
    if native is None:
        print(_NATIVE_HINT, file=sys.stderr)
        return 1
    os.execv(native, [native, *args])
    return 1  # unreachable; os.execv does not return
