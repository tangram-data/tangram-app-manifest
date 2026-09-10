"""Workspace lane for `actions` and `call` (skills-projection S3).

Lists a workspace app's declared action contracts and performs ONE action
as the calling user, over the core server's entity-action CLI surface
(`GET/POST /api/core/v1/workspaces/{ws}/apps/{app}/actions[...:perform]`),
reusing the native CLI's stored credentials. Governance is server-side
(per-type IAM, audit, confirmation-gated/irreversible refuse); the CLI
adds the same explicit `--allow-mutation` consent it requires locally.
"""

from __future__ import annotations

import urllib.parse

from .local_os import LocalOsError, base_url, load_os_credential, os_request


def wants_os_lane(args) -> bool:
    # Presence, not truthiness: `--workspace` given BARE parses to "" (the
    # ambient context fills in downstream), and an explicitly EMPTY
    # --instance/--os-url/--token must reach the selector's refusal instead
    # of silently reading as "no selector".
    return any(
        getattr(args, name, None) is not None for name in ("workspace", "instance", "os_url", "token")
    )


def _credential(selector: dict) -> dict:
    if selector.get("url"):
        return {"token": selector["token"], "url": selector["url"]}
    return load_os_credential(selector.get("instance"))


def _workspace(selector: dict, credential: dict) -> str:
    workspace = selector.get("workspace")
    if workspace:
        return workspace
    # Refinement: the refusal lists the workspaces this credential can see,
    # so the fix is one copy-paste away. Best-effort — listing failures must
    # not mask the actual error.
    visible = ""
    try:
        rows = os_request(credential, "GET", "/api/core/v1/workspaces", label="OS workspaces")
        names = sorted(
            {name for row in rows for name in [_workspace_name(row)] if name}
            if isinstance(rows, list)
            else set()
        )
        if names:
            visible = f" (visible workspaces: {', '.join(names)})"
    except LocalOsError:
        pass
    raise LocalOsError(
        "the Tangram OS lane needs a workspace: pass --workspace WS, or set an "
        f"ambient one with `tangram use INSTANCE/WS`{visible}"
    )


def _workspace_name(row) -> str | None:
    if isinstance(row, str):
        return row
    if not isinstance(row, dict):
        return None
    for path in (("name",), ("metadata", "name"), ("spec", "name"), ("workspace",)):
        value = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _actions_path(workspace: str, app: str) -> str:
    quoted_ws = urllib.parse.quote(workspace, safe="")
    quoted_app = urllib.parse.quote(app, safe="")
    return f"/api/core/v1/workspaces/{quoted_ws}/apps/{quoted_app}/actions"


def os_actions(app: str, selector: dict, *, timeout_seconds: float = 60.0) -> dict:
    """The app's declared action contracts, verbatim from the server."""
    credential = _credential(selector)
    workspace = _workspace(selector, credential)
    catalog = os_request(
        credential,
        "GET",
        _actions_path(workspace, app),
        timeout_seconds=timeout_seconds,
        label="OS actions",
    )
    rows = [
        # `ref` mirrors the local catalog rows (and feeds the TTY renderer).
        {"ref": f"{row.get('resourceType')}.{row.get('action')}", **row}
        for row in catalog.get("actions") or []
        if isinstance(row, dict)
    ]
    # Client-established provenance LAST: the server payload must not be able
    # to overwrite it.
    return {**catalog, "actions": rows, "workspace": workspace, "target": base_url(credential)}


def _resolve_ref(catalog: dict, ref: str) -> dict:
    """Same short-ref grammar as the local lane: `Action` or
    `ResourceType.Action`, ambiguity names the candidates."""
    rows = [row for row in catalog.get("actions") or [] if isinstance(row, dict)]
    matches = [
        row
        for row in rows
        if row.get("action") == ref or f"{row.get('resourceType')}.{row.get('action')}" == ref
    ]
    if len(matches) == 1:
        return matches[0]
    declared = ", ".join(sorted(f"{row.get('resourceType')}.{row.get('action')}" for row in rows))
    if not matches:
        raise LocalOsError(f"no action matches {ref!r}; declared: {declared or '(none)'}")
    choices = ", ".join(sorted(f"{row['resourceType']}.{row['action']}" for row in matches))
    raise LocalOsError(f"{ref!r} is ambiguous: {choices} — use ResourceType.Action")


def os_call(
    app: str,
    ref: str,
    arguments: dict,
    selector: dict,
    *,
    allow_mutation: bool,
    confirm: bool,
    timeout_seconds: float = 60.0,
) -> dict:
    credential = _credential(selector)
    workspace = _workspace(selector, credential)
    if confirm:
        raise LocalOsError(
            "--confirm has no effect on the OS lane: confirmation-gated actions "
            "cannot run from the CLI yet — use the app UI"
        )
    catalog = os_request(
        credential,
        "GET",
        _actions_path(workspace, app),
        timeout_seconds=timeout_seconds,
        label="OS actions",
    )
    row = _resolve_ref(catalog, ref)
    effect = row.get("effect")
    if effect != "Stateless" and not allow_mutation:
        raise LocalOsError(
            f"'{row.get('resourceType')}.{row.get('action')}' has effect {effect or 'unknown'}; "
            "pass --allow-mutation to run it"
        )
    body = {"resource_type": row["resourceType"], "action": row["action"], "args": arguments}
    performed = os_request(
        credential,
        "POST",
        _actions_path(workspace, app) + "/perform",
        body,
        timeout_seconds=timeout_seconds,
        label="OS call",
    )
    return {
        **performed,
        "workspace": workspace,
        "app": app,
        "action": f"{row.get('resourceType')}.{row.get('action')}",
    }
