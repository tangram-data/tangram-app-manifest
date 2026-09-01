"""CLI ergonomics for invoking actions: short refs, catalog, warm sessions.

Short refs let agents write `call my-app Todo.List` instead of the full
`{group}/{name}#{ResourceType}.{Action}@{operationId}` binding id;
`actions` lists an app's actions compactly; `attach_url` lets `call
--local` reuse a session already started by `run`/`open` instead of
booting a backend per call.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request


class ActionRefError(ValueError):
    """A short action reference did not resolve unambiguously."""


def resolve_action_ref(app, ref: str) -> str:
    """Expand `Action`, `ResourceType.Action`, or a full id; full ids pass through."""
    if "#" in ref:
        return ref
    matches = [
        action
        for action in app.graph.actions
        if action.name == ref or f"{action.resource_type}.{action.name}" == ref
    ]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        known = ", ".join(
            f"{action.resource_type}.{action.name}" for action in app.graph.actions
        )
        raise ActionRefError(f"no action matches {ref!r}; declared: {known or '(none)'}")
    choices = ", ".join(f"{m.resource_type}.{m.name}" for m in matches)
    raise ActionRefError(f"{ref!r} is ambiguous: {choices} — use ResourceType.Action")


def call_policy(app, binding_ref: str, *, allow_mutation: bool, confirm: bool):
    """The per-call policy for `call` flags; None keeps the read-only default.

    Grants are scoped to the ONE action being invoked: `--allow-mutation`
    permits its non-Stateless effect, `--confirm` records the human running
    the command as the approver of its confirmation gate."""
    if not (allow_mutation or confirm):
        return None
    from .policy import LocalDevelopmentPolicy

    action = app.graph.resolve(binding_ref)[0]
    return LocalDevelopmentPolicy(
        allow_mutations={action.id} if allow_mutation else frozenset(),
        preauthorized_confirmations={action.id} if confirm else frozenset(),
    )


def actions_catalog(app) -> list[dict]:
    """Compact, agent-facing listing of an app's actions."""
    rows = []
    for action in app.graph.actions:
        rows.append(
            {
                "ref": f"{action.resource_type}.{action.name}",
                "id": action.id,
                "effect": action.effect.value,
                "requiresConfirmation": action.requires_confirmation,
                "doc": action.description,
                "bindings": [binding.operation_id for binding in action.bindings],
            }
        )
    return rows


def record_session(preview: Path, backend_url: str, pid: int) -> None:
    (preview / "session.json").write_text(
        json.dumps({"backendUrl": backend_url, "pid": pid}), encoding="utf-8"
    )


def clear_session(preview: Path) -> None:
    try:
        (preview / "session.json").unlink()
    except OSError:
        pass


def attach_url(package_root: Path) -> str | None:
    """The live session's backend URL, or None (stale files never attach)."""
    marker = package_root / ".preview" / "session.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        backend_url, pid = state["backendUrl"], int(state["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned elsewhere
    parts = urllib.parse.urlsplit(backend_url)
    if parts.scheme != "http":
        return None
    try:
        if not ipaddress.ip_address(parts.hostname or "").is_loopback:
            return None
    except ValueError:
        return None  # hostnames (incl. 127.0.0.1.evil.com tricks) never attach
    try:
        with urllib.request.urlopen(f"{backend_url}/openapi.json", timeout=2):
            return backend_url
    except Exception:
        return None
