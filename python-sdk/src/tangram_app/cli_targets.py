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

from .local_os import (
    LocalOsError,
    ambient_context,
    env_workspace,
    base_url,
    configured_instances,
    load_os_credential,
    os_install,
)
from .local_store import install_app, list_installed, resolve_installed, resolve_target, uninstall_app


class TargetError(ValueError):
    """A target could not be resolved or was not consented to."""


# Bare `--workspace` (ambient-fill request). argv strings cannot contain NUL,
# so an explicit value can never collide with this sentinel.
AMBIENT_WORKSPACE = "\x00ambient"
GLOBAL_APP_WORKSPACE = "tangram_os_shared"


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _confirm(question: str) -> bool:
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def resolve_os_selector(args) -> dict:
    """Selector grammar: one complete OS target or a refusal."""
    workspace, instance = args.workspace, args.instance
    os_url, token = args.os_url, args.token
    global_install = getattr(args, "global_install", False)
    if global_install and workspace is not None:
        raise TargetError("--global conflicts with --workspace")
    # An EXPLICITLY EMPTY selector (e.g. --workspace "$UNSET_VAR") must
    # refuse, never silently fall back to the ambient context. Bare
    # `--workspace` (the AMBIENT_WORKSPACE sentinel) is the only ambient-fill
    # request.
    if workspace == AMBIENT_WORKSPACE:
        workspace = None
    elif workspace is not None and not str(workspace).strip():
        raise TargetError("--workspace must not be empty")
    for name, value in (("--instance", instance), ("--os-url", os_url), ("--token", token)):
        if value is not None and not str(value).strip():
            raise TargetError(f"{name} must not be empty")
    if global_install:
        # Global is an explicit scope; neither persisted nor session workspace
        # defaults may influence it. Instance selection and consent still apply.
        if instance in ("local", "machine"):
            raise TargetError("--global requires a Tangram OS instance, not the local machine")
        if os_url or token:
            if not (os_url and token):
                raise TargetError("--os-url and --token go together")
            if instance:
                raise TargetError("--os-url conflicts with --instance")
            return {
                "url": os_url, "token": token, "workspace": GLOBAL_APP_WORKSPACE,
                "scope": "global", "ambient": False,
            }
        if instance:
            return {
                "instance": instance, "workspace": GLOBAL_APP_WORKSPACE,
                "scope": "global", "ambient": False,
            }
        context = ambient_context(ignore_workspace=True)
        if not context["instance"]:
            raise TargetError(
                "--global requires a Tangram OS instance; pass --instance NAME "
                "or select one with `tangram use INSTANCE`"
            )
        return {
            "instance": context["instance"], "profile_url": context["profile_url"],
            "workspace": GLOBAL_APP_WORKSPACE, "scope": "global", "ambient": True,
        }
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
        # An explicit instance still honors the SESSION's env workspace
        # (TANGRAM_WORKSPACE is deliberate per-session intent, unlike the
        # persisted .HEAD workspace which belongs to the persisted instance).
        return {"instance": instance, "workspace": (workspace or None) or env_workspace(), "ambient": False}
    # Fully ambient: instance AND workspace come from ONE persisted-context
    # read (`tangram use I[/WS]`), so a concurrent `use` cannot pair this
    # read's workspace with a different instance downstream. A machine
    # context REFUSES here — downstream must never re-read ambient state to
    # fill the missing instance.
    context = ambient_context()
    if context["instance"] is None:
        instances = ", ".join(configured_instances()) or "(none)"
        raise TargetError(
            "--workspace targets a Tangram OS workspace but the resolved context is the "
            f"machine — pass --instance NAME or --workspace INSTANCE/WS (configured: "
            f"{instances}), or select one with `tangram use INSTANCE/WS`"
        )
    return {
        "instance": context["instance"],
        "workspace": (workspace or None) or context["workspace"],
        "profile_url": context["profile_url"],
        "ambient": True,
    }


def _describe(selector: dict, credential: dict | None) -> str:
    scope = (
        "global scope (shared across all workspaces)"
        if selector.get("scope") == "global"
        else f"workspace '{selector['workspace']}'"
    )
    if credential is not None:
        name = credential.get("instance", "explicit-url")
        return f"instance '{name}' ({base_url(credential)}), {scope}"
    return f"{selector.get('url')}, {scope}"


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
    if selector.get("scope") == "global":
        raise TargetError(
            f"refusing the ambient-context target ({description}) without --yes in a "
            f"non-interactive run; pass --global --instance NAME (configured: {instances}), "
            "or --yes to acknowledge this target"
        )
    raise TargetError(
        f"refusing the ambient-context target ({description}) without --yes in a "
        f"non-interactive run; pass --instance NAME / --workspace INSTANCE/WS "
        f"(configured: {instances}), or `tangram use local` for a machine install"
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
        "context": "local",
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
    # An EXPLICITLY EMPTY --workspace (e.g. an unset shell variable) must
    # refuse — never fall through to the ambient or machine lane.
    if args.workspace is not None and not str(args.workspace).strip():
        raise TargetError("--workspace must not be empty")
    if getattr(args, "global_install", False):
        selector = resolve_os_selector(args)
    elif not args.workspace:
        if any((args.instance, args.os_url, args.token)):
            raise TargetError(
                "--instance/--os-url/--token target a Tangram OS workspace; "
                "add --workspace WS (or drop them for a machine install)"
            )
        # Selector-free install follows the resolved context: the machine
        # context installs into ~/.tangram; an OS context deploys to ITS
        # workspace (consent-gated below), never silently to the local store.
        context = ambient_context()
        if not context["instance"]:
            if args.dry_run:
                raise TargetError(
                    "--dry-run previews a Tangram OS install; add --workspace WS or select an OS context"
                )
            return {"installed": _normalized(install_app(args.source, force=replace))}
        if not context["workspace"]:
            raise TargetError(
                f"the context targets instance '{context['instance']}' with no workspace — "
                f"`tangram use {context['instance']}/WS`, pass --workspace WS, or "
                "`tangram use local` for a machine install"
            )
        selector = {
            "instance": context["instance"],
            "workspace": context["workspace"],
            "profile_url": context["profile_url"],
            "ambient": True,
        }
    else:
        selector = resolve_os_selector(args)
    if selector.get("url"):
        credential = None
    else:
        credential = load_os_credential(selector["instance"], expected_url=selector.get("profile_url"))
    description = _describe(selector, credential)
    _consent_os_install(selector, description, args.yes)
    token = selector.get("token")
    if token == "-":
        token = sys.stdin.read().strip()
    # The credential the user consented to is the one that executes — no
    # re-resolution between the prompt and the POST.
    outcome = os_install(
        resolve_target(args.source),
        selector["workspace"],
        credential=credential,
        token=token,
        url=selector.get("url"),
        dry_run=args.dry_run,
        upgrade=replace,
    )
    outcome["instance"] = credential.get("instance") if credential else None
    if selector.get("scope") == "global":
        outcome["scope"] = "global"
    return {"deployed": outcome}


def _refuse_machine_verb_in_os_context(verb: str, fix: str) -> None:
    """These verbs manage the machine store only; under an OS context they
    refuse instead of silently answering from the wrong target (discovery
    must agree with execution)."""
    context = ambient_context()
    if not context["instance"]:
        return
    target = context["instance"] + (f"/{context['workspace']}" if context["workspace"] else "")
    workspace = context["workspace"] or "WS"
    raise TargetError(
        f"the context targets Tangram OS '{target}' but `app {verb}` manages this machine's "
        f"app store only — {fix.format(workspace=workspace)}, or `tangram use local` first"
    )


def handle_app_list() -> dict:
    _refuse_machine_verb_in_os_context("list", "use `tangram app ls --workspace {workspace}` for workspace apps")
    return {"apps": [_normalized(entry) for entry in list_installed()]}


def handle_app_get(ref: str) -> dict:
    _refuse_machine_verb_in_os_context("get", "use `tangram app get GROUP NAME --workspace {workspace}`")
    entry = resolve_installed(ref)
    if entry is None:
        raise TargetError(f"no installed app matches {ref!r}")
    return {"app": _normalized(entry)}


def handle_app_uninstall(args) -> dict:
    _refuse_machine_verb_in_os_context(
        "uninstall", "use `tangram app undeploy GROUP NAME --workspace {workspace}`"
    )
    entry = resolve_installed(args.ref)
    if entry is None:
        raise TargetError(f"no installed app matches {args.ref!r}")
    _consent_uninstall(entry["id"], args.yes)
    return {"uninstalled": _normalized(uninstall_app(args.ref))}
