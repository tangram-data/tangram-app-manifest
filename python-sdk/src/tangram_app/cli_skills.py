"""`skill install` dispatch: put bundled authoring skills onto agent homes."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .skills import (
    BUILDER_SKILL_NAME,
    PACKAGED_SKILLS,
    configure_codex_agents,
    install_packaged_skill,
    install_packaged_skill_codex,
    packaged_skill_codex_target,
    packaged_skill_current,
    packaged_skill_target,
)


def install_skills(args: argparse.Namespace) -> dict[str, Any]:
    """Bare `skill install` sets up the whole machine: every packaged skill
    for both Claude Code (user scope) and Codex. A NAME and/or scope flag
    narrows it, and single installs keep their original envelope."""
    named = getattr(args, "name", None)
    if args.skill_command == "install-builder":
        named = named or BUILDER_SKILL_NAME
    names = [named] if named else list(PACKAGED_SKILLS)
    if args.codex:
        scopes = ["codex"]
    elif args.user:
        scopes = ["user"]
    elif args.project is not None or named:
        # A NAME without a scope keeps the historical default (project '.').
        scopes = ["project"]
    else:
        scopes = ["user", "codex"]

    def skills_root(scope: str) -> Path:
        root = Path.home() if scope == "user" else Path(args.project or ".")
        return root / ".claude" / "skills"

    # Preflight every destination so a diverged copy refuses BEFORE anything
    # is written — a multi-install never leaves a partial setup behind.
    if not args.force:
        for scope in scopes:
            for name in names:
                target = (
                    packaged_skill_codex_target(name)
                    if scope == "codex"
                    else packaged_skill_target(name, skills_root(scope))
                )
                if (target.is_symlink() or target.exists()) and not packaged_skill_current(name, target):
                    raise FileExistsError(f"skill already installed at {target} (use --force to replace)")

    installed: list[dict[str, str]] = []
    agents: Path | None = None
    for scope in scopes:
        for name in names:
            if scope == "codex":
                target = install_packaged_skill_codex(name, force=args.force)
            else:
                target = install_packaged_skill(name, skills_root(scope), force=args.force)
            installed.append({"skill": str(target.resolve()), "scope": scope})
        if scope == "codex" and not getattr(args, "no_agents", False):
            agents = configure_codex_agents()

    if len(installed) == 1:
        result = installed[0]
        if result["scope"] == "codex":
            result["hint"] = f"invoke as /{names[0]}"
            if agents is not None:
                result["agents"] = str(agents.resolve())
                result["hint"] = (
                    f"invoke as /{names[0]}; Codex will also use Tangram skills "
                    f"automatically via {agents}"
                )
        return result
    result = {
        "installed": installed,
        "hint": "invoke as " + " or ".join(f"/{name}" for name in names),
    }
    if agents is not None:
        result["agents"] = str(agents.resolve())
    return result
