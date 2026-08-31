"""The bundled tangram-app-builder authoring skill: sync + installer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tangram_app.skills import (
    BUILDER_SKILL_NAME,
    PACKAGED_SKILLS,
    builder_skill_text,
    install_builder_skill,
    packaged_skill_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class BuilderSkillSyncTest(unittest.TestCase):
    def test_all_shipped_copies_are_identical(self):
        """Package data is the copy releases ship; the repo-autoload copies
        (.claude/skills) and the plugin copies (skills/) must never drift."""
        for name in PACKAGED_SKILLS:
            mirrors = [
                REPO_ROOT / ".claude" / "skills" / name / "SKILL.md",
                REPO_ROOT / "skills" / name / "SKILL.md",
            ]
            if not all(path.is_file() for path in mirrors):
                self.skipTest("repo mirrors absent (running from an sdist)")
            packaged = hashlib.sha256(packaged_skill_text(name).encode("utf-8")).hexdigest()
            for path in mirrors:
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    packaged,
                    f"{path} drifted from the packaged skill copy",
                )

    def test_skills_have_frontmatter(self):
        for name in PACKAGED_SKILLS:
            text = packaged_skill_text(name)
            self.assertTrue(text.startswith("---\n"), name)
            self.assertIn(f"name: {name}", text)
        with self.assertRaises(ValueError):
            packaged_skill_text("no-such-skill")


class InstallBuilderSkillTest(unittest.TestCase):
    def test_installs_refuses_overwrite_and_forces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".claude" / "skills"
            target = install_builder_skill(root)
            self.assertEqual(target, root / BUILDER_SKILL_NAME / "SKILL.md")
            self.assertEqual(target.read_text(encoding="utf-8"), builder_skill_text())
            with self.assertRaises(FileExistsError):
                install_builder_skill(root)
            target.write_text("stale", encoding="utf-8")
            install_builder_skill(root, force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), builder_skill_text())

    def test_cli_project_and_user_scopes(self):
        env_src = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "proj"
            project.mkdir()
            for arguments, expected in [
                (["--project", str(project)], project / ".claude/skills" / BUILDER_SKILL_NAME / "SKILL.md"),
            ]:
                completed = subprocess.run(
                    [sys.executable, "-m", "tangram_app", "skill", "install-builder", *arguments],
                    capture_output=True,
                    text=True,
                    env={"PYTHONPATH": env_src, "PATH": "/usr/bin:/bin"},
                )
                envelope = json.loads(completed.stdout)
                self.assertTrue(envelope["ok"], envelope)
                self.assertEqual(envelope["data"]["scope"], "project")
                self.assertEqual(Path(envelope["data"]["skill"]), expected.resolve())
                self.assertTrue(expected.is_file())

    def test_codex_scope_is_claude_free(self):
        from tangram_app import cli, skills

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            with mock.patch.object(cli.Path, "home", return_value=fake_home), mock.patch.object(
                skills.Path, "home", return_value=fake_home
            ):
                code = cli.main(["skill", "install-builder", "--codex"])
            self.assertEqual(code, 0)
            installed = fake_home / ".codex" / "prompts" / f"{BUILDER_SKILL_NAME}.md"
            self.assertEqual(installed.read_text(encoding="utf-8"), builder_skill_text())
            self.assertFalse((fake_home / ".claude").exists())

    def test_install_by_name_and_unknown_name_refuses(self):
        from tangram_app import cli

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            code = cli.main(
                ["skill", "install", "tangram-connector-builder", "--project", str(project)]
            )
            self.assertEqual(code, 0)
            installed = project / ".claude/skills/tangram-connector-builder/SKILL.md"
            self.assertEqual(
                installed.read_text(encoding="utf-8"),
                packaged_skill_text("tangram-connector-builder"),
            )
            self.assertNotEqual(cli.main(["skill", "install", "bogus", "--project", str(project)]), 0)

    def test_user_scope_targets_home(self):
        from tangram_app import cli

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            with mock.patch.object(cli.Path, "home", return_value=fake_home):
                code = cli.main(["skill", "install-builder", "--user"])
            self.assertEqual(code, 0)
            installed = fake_home / ".claude" / "skills" / BUILDER_SKILL_NAME / "SKILL.md"
            self.assertTrue(installed.is_file())


if __name__ == "__main__":
    unittest.main()
