"""Target selection + consent gates for context-addressed app verbs (v3).

Implements the CLI-consolidation plan's selector grammar and per-verb
safety matrix (docs/cli-command-inventory.md §4 in the tangram repo):
exactly one resolved target — machine, or one Tangram OS
instance/workspace — or an actionable refusal, never precedence; and
confirmation that respects the one-JSON-envelope stdout contract
(prompts go to the controlling TTY via stderr; non-interactive runs
need explicit selectors or `--yes`).
"""

from __future__ import annotations

import sys

from .local_os import LocalOsError, base_url, configured_instances, load_os_credential, os_install
from .local_store import install_app, list_installed, resolve_installed, resolve_target, uninstall_app


class TargetError(ValueError):
    """A target could not be resolved or was not consented to."""


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _confirm(question: str) -> bool:
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def resolve_os_selector(args) -> dict:
    """Selector grammar: one complete OS target or a refusal."""
    workspace, instance = args.workspace, args.instance
    os_url, token = args.os_url, args.token
    if workspace and "/" in workspace:
        qualified_instance, _, qualified_ws = workspace.partition("/")
        if not qualified_instance or not qualified_ws:
            raise TargetError(f"--workspace {workspace!r} must be INSTANCE/WORKSPACE")
        if instance:
            raise TargetError("--instance conflicts with a qualified --workspace INSTANCE/WS")
        if os_url or token:
            raise TargetError("--os-url/--token conflict with a qualified --workspace INSTANCE/WS")
        return {"instance": qualified_instance, "workspace": qualified_ws, "ambient": False}
    if os_url or token:
        if not (os_url and token):
            raise TargetError("--os-url and --token go together")
        if instance:
            raise TargetError("--os-url conflicts with --instance")
        return {"url": os_url, "token": token, "workspace": workspace, "ambient": False}
    if instance:
        return {"instance": instance, "workspace": workspace, "ambient": False}
    return {"instance": None, "workspace": workspace, "ambient": True}


def _describe(selector: dict, credential: dict | None) -> str:
    if credential is not None:
        name = credential.get("instance", "explicit-url")
        return f"instance '{name}' ({base_url(credential)}), workspace '{selector['workspace']}'"
    return f"{selector.get('url')}, workspace '{selector['workspace']}'"


def _consent_os_install(selector: dict, description: str, yes: bool) -> None:
    if yes:
        return
    if _interactive():
        if not _confirm(f"Install to {description}?"):
            raise TargetError("aborted by user")
        return
    if not selector["ambient"]:
        return  # explicit selector is deliberate in non-interactive runs
    instances = ", ".join(configured_instances()) or "(none)"
    raise TargetError(
        f"refusing the ambient-context target ({description}) without --yes in a "
        f"non-interactive run; pass --instance NAME / --workspace INSTANCE/WS "
        f"(configured: {instances}), or drop --workspace to install locally"
    )


def _consent_uninstall(what: str, yes: bool) -> None:
    if yes:
        return
    if _interactive():
        if not _confirm(f"Uninstall {what}? This deletes its installed copy and .preview state"):
            raise TargetError("aborted by user")
        return
    raise TargetError("uninstall is destructive; pass --yes in non-interactive runs")


def _normalized(entry: dict) -> dict:
    """One `ls`/`get` schema across contexts: id, version, context, state, detail."""
    return {
        "id": entry["id"],
        "version": entry.get("version"),
        "context": "machine",
        "state": "installed",
        "detail": {k: v for k, v in entry.items() if k not in ("id", "version")},
    }


def handle_app_install(args) -> dict:
    if args.force:
        print(
            "warning: --force is deprecated; use --upgrade (treated as --upgrade)",
            file=sys.stderr,
        )
    replace = args.upgrade or args.force
    if not args.workspace:
        if any((args.instance, args.os_url, args.token, args.dry_run)):
            raise TargetError(
                "--instance/--os-url/--token/--dry-run target a Tangram OS workspace; "
                "add --workspace WS (or drop them for a machine install)"
            )
        return {"installed": _normalized(install_app(args.source, force=replace))}
    selector = resolve_os_selector(args)
    if selector.get("url"):
        credential = None
    else:
        credential = load_os_credential(selector["instance"])
    description = _describe(selector, credential)
    _consent_os_install(selector, description, args.yes)
    token = selector.get("token")
    if token == "-":
        token = sys.stdin.read().strip()
    outcome = os_install(
        resolve_target(args.source),
        selector["workspace"],
        instance=credential.get("instance") if credential else None,
        token=token,
        url=selector.get("url"),
        dry_run=args.dry_run,
        upgrade=replace,
    )
    outcome["instance"] = credential.get("instance") if credential else None
    return {"deployed": outcome}


def handle_app_list() -> dict:
    return {"apps": [_normalized(entry) for entry in list_installed()]}


def handle_app_get(ref: str) -> dict:
    entry = resolve_installed(ref)
    if entry is None:
        raise TargetError(f"no installed app matches {ref!r}")
    return {"app": _normalized(entry)}


def handle_app_uninstall(args) -> dict:
    entry = resolve_installed(args.ref)
    if entry is None:
        raise TargetError(f"no installed app matches {args.ref!r}")
    _consent_uninstall(entry["id"], args.yes)
    return {"uninstalled": _normalized(uninstall_app(args.ref))}
