"""Argument-parser definition for the tangram-app CLI (split from cli.py)."""

from __future__ import annotations

from .cli import _Parser


def build_parser() -> _Parser:
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
    app_install.add_argument("--force", action="store_true")  # deprecated → --upgrade
    app_install.add_argument("--workspace")
    app_install.add_argument("--instance")
    app_install.add_argument("--os-url")
    app_install.add_argument("--token")
    app_install.add_argument("--dry-run", action="store_true")
    app_install.add_argument("--upgrade", action="store_true")
    app_install.add_argument("--yes", action="store_true")
    app_commands.add_parser("list")
    app_get = app_commands.add_parser("get")
    app_get.add_argument("ref")
    app_uninstall = app_commands.add_parser("uninstall")
    app_uninstall.add_argument("ref")
    app_uninstall.add_argument("--yes", action="store_true")

    actions = commands.add_parser("actions")
    actions.add_argument("target")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--fix", action="store_true")

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


