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

from tangram_app.skills import BUILDER_SKILL_NAME, builder_skill_text, install_builder_skill

REPO_ROOT = Path(__file__).resolve().parents[2]


class BuilderSkillSyncTest(unittest.TestCase):
    def test_all_shipped_copies_are_identical(self):
        """Package data is the copy releases ship; the repo-autoload copy
        (.claude/skills) and the plugin copy (skills/) must never drift."""
        mirrors = [
            REPO_ROOT / ".claude" / "skills" / BUILDER_SKILL_NAME / "SKILL.md",
            REPO_ROOT / "skills" / BUILDER_SKILL_NAME / "SKILL.md",
        ]
        if not all(path.is_file() for path in mirrors):
            self.skipTest("repo mirrors absent (running from an sdist)")
        packaged = hashlib.sha256(builder_skill_text().encode("utf-8")).hexdigest()
        for path in mirrors:
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                packaged,
                f"{path} drifted from the packaged skill copy",
            )

    def test_skill_has_frontmatter(self):
        text = builder_skill_text()
        self.assertTrue(text.startswith("---\n"))
        self.assertIn(f"name: {BUILDER_SKILL_NAME}", text)


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
