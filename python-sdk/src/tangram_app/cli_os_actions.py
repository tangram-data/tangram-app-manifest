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
    return bool(
        getattr(args, "workspace", None)
        or getattr(args, "instance", None)
        or getattr(args, "os_url", None)
        or getattr(args, "token", None)
    )


def _credential(selector: dict) -> dict:
    if selector.get("url"):
        return {"token": selector["token"], "url": selector["url"]}
    return load_os_credential(selector.get("instance"))


def _workspace(selector: dict) -> str:
    workspace = selector.get("workspace")
    if not workspace:
        raise LocalOsError("the Tangram OS lane needs --workspace WS (or --workspace INSTANCE/WS)")
    return workspace


def _actions_path(workspace: str, app: str) -> str:
    quoted_ws = urllib.parse.quote(workspace, safe="")
    quoted_app = urllib.parse.quote(app, safe="")
    return f"/api/core/v1/workspaces/{quoted_ws}/apps/{quoted_app}/actions"


def os_actions(app: str, selector: dict, *, timeout_seconds: float = 60.0) -> dict:
    """The app's declared action contracts, verbatim from the server."""
    credential = _credential(selector)
    workspace = _workspace(selector)
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
    workspace = _workspace(selector)
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
