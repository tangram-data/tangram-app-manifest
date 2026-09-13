"""Stable agent-facing discovery, context and invocation entry point."""
from __future__ import annotations

import json
import re
import urllib.parse
import uuid

from .app import TangramApp
from .hub_target import HubError, load, resolve
from .local_os import os_request
from .local_store import list_installed, resolve_installed


def add_parser(commands, app_commands):
    context = commands.add_parser("context", help="Resolve a pinned execution context")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    pin = context_commands.add_parser("resolve")
    pin.set_defaults(capability_command="resolve")
    knowledge = commands.add_parser("knowledge", help="Search and read workspace knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    pin.add_argument("--local", action="store_true")
    pin.add_argument("--instance")
    pin.add_argument("--workspace")
    pin.set_defaults(os_url=None, token=None)
    for verb in ("search", "inspect", "call", "invocation", "knowledge-search", "knowledge-read"):
        group = knowledge_commands if verb.startswith("knowledge-") else app_commands
        p = group.add_parser(verb.removeprefix("knowledge-"))
        p.set_defaults(capability_command=verb)
        p.add_argument("--target-ref", required=True)
        p.add_argument("--timeout", type=float, default=300 if verb == "call" else 30)
        if verb in ("search", "knowledge-search"):
            p.add_argument("query")
            p.add_argument("--limit", type=int, default=10)
            p.add_argument("--offset", type=int, default=0)
        if verb in ("inspect", "call"):
            p.add_argument("action", help="Canonical app#Resource.Action[@operation] from discovery")
            p.add_argument("--contract", required=True)
        if verb == "call":
            p.add_argument("--input-json", default="-")
            p.add_argument("--invocation-id", required=True, help="Unique UUID; reuse for status/replay, never change inputs")
            p.add_argument("--allow-mutation", action="store_true")
            p.add_argument("--approve", action="store_true", help="Explicit consent to this exact request; not independent human approval proof")
        if verb == "invocation":
            p.add_argument("invocation_id")
        if verb == "knowledge-read":
            p.add_argument("source_id")
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--limit", type=int, default=12000)


def _parts(action: str) -> tuple[str, str]:
    app, sep, ref = action.partition("#")
    if not sep or not app or not ref:
        raise HubError("invalid_action", "use the canonical app#Resource.Action from discovery")
    return app, ref


def _installed(app_id: str):
    entry = resolve_installed(app_id)
    if entry is None or entry["id"] != app_id:
        raise HubError("app_not_found", "canonical app ID is not installed in this target")
    # Resolve through the store, never cwd: a same-named directory is not this app.
    return entry, TangramApp.from_package(entry["root"])


def _local_inspect(action_id: str, expected: str):
    app_id, _ = _parts(action_id)
    entry, app = _installed(app_id)
    if app.graph.package.digest != expected:
        raise HubError("contract_changed", "app contract changed; search and inspect again")
    action, binding = app.graph.resolve(action_id)
    return entry, app, action, binding


def _tokens(text: str) -> set[str]:
    return {word.rstrip("s") for word in re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
            if len(word) > 1 and word not in {"show", "me", "my", "the", "a", "an", "please"}}


def _local_search(query: str, limit: int, offset: int) -> dict:
    wanted = _tokens(query)
    rows = []
    unavailable = []
    for entry in list_installed():
        # Installed projection is catalog metadata, not authorization or executable schema.
        block = entry.get("projection") or {}
        freeform = block.get("freeform") or {}
        candidates = entry.get("capabilities")
        if candidates is None:
            try:
                _, app = _installed(entry["id"])
            except Exception:
                unavailable.append({"app": entry["id"], "code": "catalog_unavailable"})
                continue
            candidates = [{"action": tool.id, "description": tool.description, "effect": tool.effect,
                           "requiresConfirmation": tool.requires_confirmation, "contract": app.graph.package.digest}
                          for tool in app.tools()]
        for item in candidates:
            description = item.get("description", "")
            text = f"{entry['id']} {item['action']} {description} {freeform.get('description', '')}"
            score = len(wanted & _tokens(text))
            if wanted and not score:
                continue
            rows.append({**item, "score": score, "descriptionTrust": "app-provided-data"})
    rows.sort(key=lambda row: (-row["score"], row["action"]))
    return {"candidates": rows[offset:offset + limit],
            "nextOffset": offset + limit if offset + limit < len(rows) else None,
            "unavailableApps": unavailable}


def _local_call(args, target):
    from . import hub_journal
    from .cli import _read_json_input, _run, _error_details
    from .cli_parser import build_parser
    from .schema import validate

    entry, app, action, binding = _local_inspect(args.action, args.contract)
    arguments = _read_json_input(args.input_json)
    validate(arguments, binding.input_schema)
    if action.effect.value != "Stateless" or action.requires_confirmation:
        from .desktop_invocation import call as desktop_call
        # Resolve the optional @operation suffix before freezing the desktop
        # request so the worker and approver bind the same canonical action.
        from copy import copy
        canonical = copy(args)
        canonical.action = binding.id
        return desktop_call(canonical, arguments)
    # Existing call owns policy, request rendering, schema validation, audit and runtime.
    connector = app.graph.runtime_requirements.get("backend") == "connector"
    argv = ["call", entry["root"], binding.id, "--connected" if connector else "--local",
            "--package-digest", args.contract, "--input-json", json.dumps(arguments),
            "--timeout", str(args.timeout)]
    if args.allow_mutation:
        argv.append("--allow-mutation")
    if args.approve:
        argv.append("--confirm")
    parsed = build_parser().parse_args(argv)
    record, execute = hub_journal.claim(args.invocation_id,
        {"target": target, "action": binding.id, "contract": args.contract, "input": arguments},
        gated=action.requires_confirmation, approved=args.approve)
    if not execute:
        return record
    try:
        result = _run(parsed)
    except KeyboardInterrupt:
        return hub_journal.complete(record, "unknown", error={"code": "interrupted", "message": "interrupted; reconcile before retrying", "retryable": False})
    except Exception as error:
        code, _, message = _error_details(error)
        if code in {"local_runtime_failed", "invalid_input", "capability_graph_stale", "policy_denied", "confirmation_required"}:
            return hub_journal.complete(record, "failed", error={"code": code, "message": message, "retryable": True})
        # Conservative: even output validation or transport failure can follow a committed write.
        return hub_journal.complete(record, "unknown", error={"code": code, "message": message, "retryable": False})
    return hub_journal.complete(record, "succeeded", result=result)


def run(args):
    if args.capability_command == "resolve":
        return resolve(args)
    target, credential = load(args.target_ref)
    verb = args.capability_command
    if args.timeout <= 0 or args.timeout > 3600:
        raise HubError("invalid_arguments", "timeout must be between 0 and 3600 seconds")
    if hasattr(args, "limit"):
        maximum = 50000 if verb == "knowledge-read" else 100
        if not 1 <= args.limit <= maximum or args.offset < 0:
            raise HubError("invalid_arguments", f"limit must be 1..{maximum}; offset must be nonnegative")
    if verb in ("call", "invocation"):
        try:
            args.invocation_id = str(uuid.UUID(args.invocation_id))
        except ValueError:
            raise HubError("invalid_arguments", "invocation ID must be a UUID") from None
    if credential is None:
        if verb == "search":
            result = _local_search(args.query, args.limit, args.offset)
        elif verb == "inspect":
            _, app, action, binding = _local_inspect(args.action, args.contract)
            result = {"action": binding.id, "contract": args.contract, "effect": action.effect.value,
                      "requiresConfirmation": action.requires_confirmation,
                      "inputSchema": binding.to_dict()["inputSchema"], "outputSchema": binding.to_dict().get("outputSchema")}
        elif verb == "call":
            result = _local_call(args, target)
        elif verb == "invocation":
            from .hub_journal import get
            from .desktop_invocation import status as desktop_status
            result = desktop_status(args)
            if result is None:
                result = get(args.invocation_id, target)
        else:
            raise HubError("unsupported_capability", "knowledge retrieval requires a workspace target")
    else:
        ws = urllib.parse.quote(target["workspace"], safe="")
        base = f"/api/core/v1/workspaces/{ws}/hub"
        if verb == "knowledge-search":
            if args.offset:
                raise HubError("unsupported_pagination", "knowledge search currently supports bounded results, not offsets")
            result = os_request(credential, "POST", "/api/core/v1/knowledge/search",
                                {"workspace": target["workspace"], "query": args.query, "limit": args.limit},
                                timeout_seconds=args.timeout, label="knowledge search")
        elif verb == "knowledge-read":
            try:
                uuid.UUID(args.source_id)
            except ValueError:
                raise HubError("invalid_arguments", "source ID must be a knowledge document UUID") from None
            path = f"/api/core/v1/knowledge/documents/{args.source_id}/text?workspace={ws}"
            document = os_request(credential, "GET", path, timeout_seconds=args.timeout, label="knowledge read")
            if document is None:
                raise HubError("source_not_found", "no readable extracted text for this source")
            text = document.get("text") if isinstance(document, dict) else None
            if not isinstance(text, str):
                raise HubError("source_not_found", "no readable extracted text for this source")
            result = {"sourceId": args.source_id, "text": text[args.offset:args.offset + args.limit],
                      "offset": args.offset, "nextOffset": args.offset + args.limit if args.offset + args.limit < len(text) else None,
                      "provenance": {key: value for key, value in document.items() if key != "text"}}
        elif verb == "invocation":
            result = os_request(credential, "GET", f"{base}/invocations/{args.invocation_id}",
                                timeout_seconds=args.timeout, label="invocation status")
        else:
            body = {"query": args.query, "limit": args.limit, "offset": args.offset} if verb == "search" else {
                "action": args.action, "contract": args.contract}
            if verb == "call":
                from .cli import _read_json_input
                body.update(input=_read_json_input(args.input_json), invocationId=args.invocation_id,
                            allowMutation=args.allow_mutation, approve=args.approve)
            result = os_request(credential, "POST", f"{base}/{verb}", body,
                                timeout_seconds=args.timeout, label=f"{args.command} {verb.removeprefix('knowledge-')}")
    return {**result, "target": target, "targetRef": args.target_ref}
