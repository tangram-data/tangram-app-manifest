"""Machine-readable command surface for developers and generated agent skills."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Sequence

from .app import TangramApp
from .errors import (
    AmbiguousActionError,
    CapabilityGraphError,
    CapabilityGraphStaleError,
    ConfirmationRequiredError,
    DriverError,
    HttpResponseError,
    InputValidationError,
    LocalRuntimeError,
    ManifestCompilationError,
    ManifestDecodeError,
    ManifestValidationError,
    OutputValidationError,
    PklEvaluationError,
    PolicyDeniedError,
    RequestRenderError,
    TangramAppError,
    UnknownBindingError,
    UnsupportedRequirementError,
)
from .cli_actions import actions_catalog, attach_url, call_policy, resolve_action_ref
from .cli_connections import connected_call, handle_connect, handle_disconnect
from .local_os import os_install
from .local_store import install_app, list_installed, resolve_target, uninstall_app
from .skills import (
    BUILDER_SKILL_NAME,
    generate_skill,
    install_packaged_skill,
    install_packaged_skill_codex,
    verify_skill,
)
from .project import TangramProject


COMMAND_SCHEMA_VERSION = "1"


class CliArgumentsError(ValueError):
    pass


class CliHelp(Exception):
    def __init__(self, help_text: str):
        self.help_text = help_text
        super().__init__(help_text)


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        raise CliHelp(parser.format_help())


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-h",
            "--help",
            action=_HelpAction,
            help="show this help message",
        )

    def error(self, message: str) -> None:
        raise CliArgumentsError(message)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command in ("run", "open"):
            return _run_local_foreground(args)
        data = _run(args)
        _emit({"schemaVersion": COMMAND_SCHEMA_VERSION, "ok": True, "data": data})
        return 0
    except CliHelp as help_request:
        _emit(
            {
                "schemaVersion": COMMAND_SCHEMA_VERSION,
                "ok": True,
                "data": {"help": help_request.help_text},
            }
        )
        return 0
    except Exception as error:  # The CLI boundary always returns one JSON document.
        code, status, message = _error_details(error)
        _emit(
            {
                "schemaVersion": COMMAND_SCHEMA_VERSION,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )
        return status


def skill_runner_main(skill_root: str | Path, argv: Sequence[str]) -> int:
    try:
        graph = verify_skill(skill_root)
        args = _skill_parser().parse_args(argv)
        app = TangramApp.from_graph(graph)
        if args.command == "inspect":
            data = _inspect_data(app, args.binding)
        else:
            arguments = _read_json_input("-")
            if args.local_package:
                source_app = TangramApp.from_package(args.local_package)
                if source_app.graph.package.digest != graph.package.digest:
                    raise CapabilityGraphStaleError(
                        "local package no longer matches this generated skill"
                    )
                with source_app.run_local(
                    python=args.python,
                    startup_timeout_seconds=args.startup_timeout,
                    request_timeout_seconds=args.timeout,
                    audit_path=args.audit_path,
                ) as running:
                    result = asyncio.run(running.call(args.binding, arguments))
            else:
                bound = app.bind(
                    backend=args.backend,
                    audit_path=args.audit_path,
                    timeout_seconds=args.timeout,
                )
                result = asyncio.run(bound.call(args.binding, arguments))
            data = {"bindingId": graph.resolve(args.binding)[1].id, "result": result}
        _emit({"schemaVersion": COMMAND_SCHEMA_VERSION, "ok": True, "data": data})
        return 0
    except CliHelp as help_request:
        _emit(
            {
                "schemaVersion": COMMAND_SCHEMA_VERSION,
                "ok": True,
                "data": {"help": help_request.help_text},
            }
        )
        return 0
    except Exception as error:
        code, status, message = _error_details(error)
        _emit(
            {
                "schemaVersion": COMMAND_SCHEMA_VERSION,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )
        return status


def _parser() -> _Parser:
    parser = _Parser(prog="tangram-app")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("package")
    build.add_argument("--output")

    validate = commands.add_parser("validate")
    validate.add_argument("package")

    run = commands.add_parser("run")
    run.add_argument("package")
    run.add_argument("--python")
    run.add_argument("--startup-timeout", type=float, default=30.0)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--audit-path")

    inspect = commands.add_parser("inspect")
    inspect.add_argument("target")
    selection = inspect.add_mutually_exclusive_group()
    selection.add_argument("--tools", action="store_true")
    selection.add_argument("--action")
    inspect.add_argument("--format", choices=("json",), default="json")

    call = commands.add_parser("call")
    call.add_argument("target")
    call.add_argument("binding")
    execution = call.add_mutually_exclusive_group(required=True)
    execution.add_argument("--backend")
    execution.add_argument("--local", action="store_true")
    execution.add_argument("--connected", action="store_true")
    call.add_argument("--endpoint")
    call.add_argument("--allow-mutation", action="store_true")
    call.add_argument("--confirm", action="store_true")
    call.add_argument("--input-json", default="-")
    call.add_argument("--audit-path")
    call.add_argument("--timeout", type=float, default=30.0)
    call.add_argument("--startup-timeout", type=float, default=30.0)
    call.add_argument("--python")

    app_group = commands.add_parser("app")
    app_commands = app_group.add_subparsers(dest="app_command", required=True)
    app_install = app_commands.add_parser("install")
    app_install.add_argument("source")
    app_install.add_argument("--force", action="store_true")
    app_install.add_argument("--workspace")
    app_install.add_argument("--instance")
    app_install.add_argument("--os-url")
    app_install.add_argument("--token")
    app_install.add_argument("--dry-run", action="store_true")
    app_install.add_argument("--upgrade", action="store_true")
    app_commands.add_parser("list")
    app_uninstall = app_commands.add_parser("uninstall")
    app_uninstall.add_argument("ref")

    actions = commands.add_parser("actions")
    actions.add_argument("target")

    connect = commands.add_parser("connect")
    connect.add_argument("target")
    mode = connect.add_mutually_exclusive_group(required=True)
    mode.add_argument("--token")
    mode.add_argument("--oauth", action="store_true")
    connect.add_argument("--tenant")
    connect.add_argument("--client-id")
    connect.add_argument("--client-secret")
    connect.add_argument("--no-browser", action="store_true")
    connect.add_argument("--oauth-timeout", type=float, default=300.0)
    commands.add_parser("disconnect").add_argument("target")

    open_cmd = commands.add_parser("open")
    open_cmd.add_argument("package")
    open_cmd.add_argument("--no-browser", action="store_true")
    open_cmd.add_argument("--audit-path")
    open_cmd.add_argument("--timeout", type=float, default=30.0)
    open_cmd.add_argument("--startup-timeout", type=float, default=60.0)
    open_cmd.add_argument("--python")

    skill = commands.add_parser("skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    generate = skill_commands.add_parser("generate")
    generate.add_argument("target")
    generate.add_argument("--output", required=True)
    generate.add_argument("--name")
    for verb, has_name in (("install", True), ("install-builder", False)):
        install = skill_commands.add_parser(verb)
        if has_name:
            install.add_argument("name")
        scope = install.add_mutually_exclusive_group()
        scope.add_argument("--project", default=".")
        scope.add_argument("--user", action="store_true")
        scope.add_argument("--codex", action="store_true")
        install.add_argument("--force", action="store_true")
    return parser


def _skill_parser() -> _Parser:
    parser = _Parser(prog="tangram-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("binding", nargs="?")
    call = commands.add_parser("call")
    call.add_argument("binding")
    execution = call.add_mutually_exclusive_group(required=True)
    execution.add_argument("--backend")
    execution.add_argument("--local-package")
    call.add_argument("--audit-path")
    call.add_argument("--timeout", type=float, default=30.0)
    call.add_argument("--startup-timeout", type=float, default=30.0)
    call.add_argument("--python")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "validate":
        result = TangramProject.open(resolve_target(args.package)).validate()
        return {
            "valid": result.valid,
            "findings": [_finding(item) for item in result.findings],
        }
    if args.command == "build":
        app = TangramApp.from_package(resolve_target(args.package))
        output = (
            Path(args.output)
            if args.output
            else Path(args.package) / "dist/tangram-app.json"
        )
        app.graph.write_file(output)
        return {
            "authority": app.authority,
            "graph": str(output.resolve()),
            "packageDigest": app.graph.package.digest,
            "findings": [_finding(item) for item in app.findings],
        }
    if args.command == "inspect":
        app = _load_app(args.target)
        if args.action:
            return _inspect_data(app, args.action)
        data: dict[str, Any] = {
            "package": app.graph.package.to_dict(),
            "capabilityReport": app.capabilities(),
            "ui": app.ui(),
            "findings": [_finding(item) for item in app.findings],
        }
        if args.tools:
            data["tools"] = [tool.to_dict() for tool in app.tools()]
        return data
    if args.command == "connect":
        root = resolve_target(args.target)
        app = _load_app(root)
        return handle_connect(args, root, app.graph.package.id)
    if args.command == "disconnect":
        app = _load_app(resolve_target(args.target))
        return handle_disconnect(app.graph.package.id)
    if args.command == "actions":
        app = _load_app(args.target)
        return {"app": app.graph.package.id, "actions": actions_catalog(app)}
    if args.command == "call":
        arguments = _read_json_input(args.input_json)
        app = _load_app(args.target)
        args.binding = resolve_action_ref(app, args.binding)
        policy = call_policy(
            app, args.binding, allow_mutation=args.allow_mutation, confirm=args.confirm
        )
        if args.connected:
            return connected_call(
                args, app, resolve_target(args.target), arguments, policy=policy
            )
        if args.local:
            attached = attach_url(resolve_target(args.target))
            if attached is not None:
                # `run`/`open` already holds a live session — reuse it instead
                # of booting a backend per call.
                bound = app.bind(
                    backend=attached,
                    policy=policy,
                    audit_path=args.audit_path,
                    timeout_seconds=args.timeout,
                )
                result = asyncio.run(bound.call(args.binding, arguments))
                return {
                    "bindingId": bound.graph.resolve(args.binding)[1].id,
                    "result": result,
                    "session": "attached",
                }
        if args.local:
            if app.source_root is None:
                raise CliArgumentsError("--local requires a source package directory")
            with app.run_local(
                python=args.python,
                policy=policy,
                startup_timeout_seconds=args.startup_timeout,
                request_timeout_seconds=args.timeout,
                audit_path=args.audit_path,
            ) as running:
                result = asyncio.run(running.call(args.binding, arguments))
                binding_id = running.app.graph.resolve(args.binding)[1].id
        else:
            bound = app.bind(
                backend=args.backend,
                policy=policy,
                audit_path=args.audit_path,
                timeout_seconds=args.timeout,
            )
            result = asyncio.run(bound.call(args.binding, arguments))
            binding_id = bound.graph.resolve(args.binding)[1].id
        return {"bindingId": binding_id, "result": result}
    if args.command == "app" and args.app_command == "install":
        if not args.workspace and any(
            (args.instance, args.os_url, args.token, args.dry_run, args.upgrade)
        ):
            raise CliArgumentsError(
                "--instance/--os-url/--token/--dry-run/--upgrade target a Tangram OS "
                "workspace; add --workspace WS (or drop them for a user-store install)"
            )
        if args.workspace:
            token = sys.stdin.read().strip() if args.token == "-" else args.token
            return {
                "deployed": os_install(
                    resolve_target(args.source),
                    args.workspace,
                    instance=args.instance,
                    token=token,
                    url=args.os_url,
                    dry_run=args.dry_run,
                    upgrade=args.upgrade,
                )
            }
        return {"installed": install_app(args.source, force=args.force)}
    if args.command == "app" and args.app_command == "list":
        return {"apps": list_installed()}
    if args.command == "app" and args.app_command == "uninstall":
        return {"uninstalled": uninstall_app(args.ref)}
    if args.command == "skill" and args.skill_command == "generate":
        app = _load_app(args.target)
        root = generate_skill(app, args.output, skill_name=args.name)
        return {
            "authority": app.authority,
            "skill": str(root.resolve()),
            "packageDigest": app.graph.package.digest,
            "bindings": len(app.tools()),
        }
    if args.command == "skill" and args.skill_command in ("install", "install-builder"):
        name = getattr(args, "name", None) or BUILDER_SKILL_NAME
        if args.codex:
            target = install_packaged_skill_codex(name, force=args.force)
            return {
                "skill": str(target.resolve()),
                "scope": "codex",
                "hint": f"invoke as /{name}; for automatic use, tell "
                "~/.codex/AGENTS.md to read this file for Tangram tasks",
            }
        if args.user:
            skills_root = Path.home() / ".claude" / "skills"
            scope = "user"
        else:
            skills_root = Path(args.project) / ".claude" / "skills"
            scope = "project"
        target = install_packaged_skill(name, skills_root, force=args.force)
        return {"skill": str(target.resolve()), "scope": scope}
    raise CliArgumentsError("unsupported command")


def _run_local_foreground(args: argparse.Namespace) -> int:
    app = TangramApp.from_package(resolve_target(args.package))
    session = app.run_local(
        python=args.python,
        startup_timeout_seconds=args.startup_timeout,
        request_timeout_seconds=args.timeout,
        audit_path=args.audit_path,
    )
    previous_term = signal.getsignal(signal.SIGTERM)

    def stop_requested(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    try:
        _emit(
            {
                "schemaVersion": COMMAND_SCHEMA_VERSION,
                "ok": True,
                "data": {
                    "package": app.graph.package.to_dict(),
                    "runtime": "local-source",
                    "backendUrl": session.backend_url,
                    "uiUrl": session.ui_url,
                    "log": str(session.log_path),
                },
            }
        )
        if args.command == "open" and not args.no_browser:
            import webbrowser

            webbrowser.open(session.ui_url or session.backend_url)
        while session.process.poll() is None:
            time.sleep(0.25)
        return 3
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()
        signal.signal(signal.SIGTERM, previous_term)


def _load_app(target: str | Path) -> TangramApp:
    path = resolve_target(target)
    if not path.exists():
        raise FileNotFoundError(
            f"Tangram package, graph, or installed app does not exist: {path}"
        )
    return (
        TangramApp.from_package(path) if path.is_dir() else TangramApp.from_graph(path)
    )


def _inspect_data(app: TangramApp, id: str | None) -> dict[str, Any]:
    if id is None:
        return {
            "package": app.graph.package.to_dict(),
            "capabilityReport": app.capabilities(),
            "ui": app.ui(),
            "tools": [tool.to_dict() for tool in app.tools()],
        }
    action, binding = app.graph.resolve(id)
    tool = next(tool for tool in app.tools() if tool.id == binding.id)
    return {
        "package": app.graph.package.to_dict(),
        "capabilityReport": app.capabilities(),
        "ui": app.ui(),
        "action": action.to_dict(),
        "tool": tool.to_dict(),
    }


def _read_json_input(value: str) -> Any:
    try:
        text = sys.stdin.read() if value == "-" else value
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise CliArgumentsError(f"input is not valid JSON: {error.msg}") from error
    if not isinstance(decoded, dict):
        raise CliArgumentsError("input JSON must be an object")
    return decoded


def _finding(value) -> dict[str, str]:
    return {
        "severity": value.severity.value,
        "code": value.code,
        "path": value.path,
        "message": value.message,
    }


def _error_details(error: Exception) -> tuple[str, int, str]:
    if isinstance(error, CliArgumentsError):
        return "invalid_arguments", 2, str(error)
    if isinstance(error, CapabilityGraphStaleError):
        return "capability_graph_stale", 3, str(error)
    if isinstance(error, UnknownBindingError):
        return "unknown_binding", 3, str(error)
    if isinstance(error, AmbiguousActionError):
        return "ambiguous_action", 3, str(error)
    if isinstance(error, InputValidationError):
        return "invalid_input", 3, str(error)
    if isinstance(error, RequestRenderError):
        return "invalid_input", 3, str(error)
    if isinstance(error, ConfirmationRequiredError):
        return "confirmation_required", 3, str(error)
    if isinstance(error, PolicyDeniedError):
        return "policy_denied", 3, str(error)
    if isinstance(error, UnsupportedRequirementError):
        return "unsupported_requirement", 3, str(error)
    if isinstance(error, LocalRuntimeError):
        return "local_runtime_failed", 3, str(error)
    if isinstance(error, (HttpResponseError, OutputValidationError, DriverError)):
        return "upstream_failed", 3, str(error)
    if isinstance(error, CapabilityGraphError):
        return "graph_invalid", 2, str(error)
    if isinstance(
        error,
        (
            ManifestCompilationError,
            ManifestDecodeError,
            ManifestValidationError,
            PklEvaluationError,
        ),
    ):
        return "manifest_invalid", 2, str(error)
    if isinstance(error, (FileExistsError, FileNotFoundError, ValueError)):
        return "invalid_arguments", 2, str(error)
    if isinstance(error, TangramAppError):
        return "internal_error", 1, "Tangram SDK operation failed"
    return "internal_error", 1, "unexpected Tangram SDK failure"


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
