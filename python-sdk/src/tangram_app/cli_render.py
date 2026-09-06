"""Human-readable rendering for TTY sessions.

The CLI's machine contract is untouched: whenever stdout is NOT a tty
(agents, scripts, pipes, CI) — or `--json` is passed — the single JSON
envelope is emitted exactly as before. On an interactive terminal the
same data renders as text: curated layouts for the most human-facing
commands, indented JSON for the rest.
"""

from __future__ import annotations

import json
from typing import Any


def render_data(command: str | None, data: dict[str, Any]) -> str:
    if command == "doctor":
        return _doctor(data)
    if command == "app" and "apps" in data:
        return _apps(data["apps"])
    if command == "app" and "app" in data:
        return _generic(data["app"])
    if command == "validate":
        return _validate(data)
    if command == "actions":
        return _actions(data)
    if "help" in data and isinstance(data["help"], str):
        return data["help"].rstrip()
    return _generic(data)


def render_error(error: dict[str, Any]) -> str:
    return f"error [{error.get('code', 'unknown')}]: {error.get('message', '')}"


def _doctor(data: dict[str, Any]) -> str:
    lines = []
    for applied in data.get("applied", []):
        lines.append(f"fixed: {applied.get('check')} — {applied.get('action')}")
    for check in data.get("checks", []):
        mark = "ok " if check.get("ok") else ("!! " if check.get("required") else "-- ")
        line = f"  {mark}{check.get('name')}"
        if check.get("detail"):
            line += f"  ({check['detail']})"
        lines.append(line)
        if check.get("hint"):
            lines.append(f"       hint: {check['hint']}")
    lines.append("environment ok" if data.get("ok") else "environment NOT ready")
    return "\n".join(lines)


def _apps(apps: list) -> str:
    if not apps:
        return "no apps installed (tangram-app app install <source>)"
    lines = []
    for app in apps:
        lines.append(
            f"  {app.get('id')}  {app.get('version') or '-'}  "
            f"[{app.get('context')}: {app.get('state')}]"
        )
    return "\n".join(lines)


def _validate(data: dict[str, Any]) -> str:
    lines = []
    for finding in data.get("findings", []):
        severity = finding.get("severity", "?")
        if severity == "ok":
            continue
        lines.append(
            f"  {severity.upper():8} {finding.get('path', '')}  {finding.get('message', '')}"
        )
    lines.append("valid" if data.get("valid") else "INVALID")
    return "\n".join(lines)


def _actions(data: dict[str, Any]) -> str:
    lines = [f"{data.get('app')}:"]
    for action in data.get("actions", []):
        gate = " (confirmation)" if action.get("requiresConfirmation") else ""
        lines.append(f"  {action.get('ref')}  [{action.get('effect')}]{gate}")
        if action.get("doc"):
            lines.append(f"      {action['doc']}")
    return "\n".join(lines)


def _generic(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)
