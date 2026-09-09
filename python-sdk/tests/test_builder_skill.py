"""The bundled tangram-app-builder authoring skill: sync + installer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
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
            # Identical copy: reinstall is an idempotent no-op.
            self.assertEqual(install_builder_skill(root), target)
            # Diverged copy: refuse without --force.
            target.write_text("stale", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                install_builder_skill(root)
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
        from tangram_app import cli, cli_skills

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            with mock.patch.object(cli_skills.Path, "home", return_value=fake_home):
                code = cli.main(["skill", "install-builder", "--user"])
            self.assertEqual(code, 0)
            installed = fake_home / ".claude" / "skills" / BUILDER_SKILL_NAME / "SKILL.md"
            self.assertTrue(installed.is_file())

    def _bare_install(self, argv):
        from tangram_app import cli, cli_skills, skills

        directory = tempfile.mkdtemp()
        fake_home = Path(directory)
        stdout = StringIO()
        with mock.patch.object(cli_skills.Path, "home", return_value=fake_home), mock.patch.object(
            skills.Path, "home", return_value=fake_home
        ):
            with redirect_stdout(stdout):
                code = cli.main(argv)
        payload = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        return code, fake_home, payload

    def test_bare_install_sets_up_claude_and_codex(self):
        code, home, payload = self._bare_install(["skill", "install"])
        self.assertEqual(code, 0)
        for name in ("tangram-app-builder", "tangram-connector-builder"):
            self.assertTrue((home / ".claude" / "skills" / name / "SKILL.md").is_file())
            self.assertTrue((home / ".codex" / "prompts" / f"{name}.md").is_file())
        installed = payload["data"]["installed"]
        self.assertEqual(len(installed), 4)
        self.assertEqual({row["scope"] for row in installed}, {"user", "codex"})
        self.assertIn("agents", payload["data"])
        self.assertTrue((home / ".codex" / "AGENTS.md").is_file())

    def test_bare_install_is_rerunnable(self):
        from tangram_app import cli, cli_skills, skills

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            with mock.patch.object(cli_skills.Path, "home", return_value=fake_home), mock.patch.object(
                skills.Path, "home", return_value=fake_home
            ):
                self.assertEqual(cli.main(["skill", "install"]), 0)
                self.assertEqual(cli.main(["skill", "install"]), 0)

    def test_bare_install_preflights_before_writing_anything(self):
        from tangram_app import cli, cli_skills, skills

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            diverged = fake_home / ".codex" / "prompts" / "tangram-connector-builder.md"
            diverged.parent.mkdir(parents=True)
            diverged.write_text("locally edited", encoding="utf-8")
            with mock.patch.object(cli_skills.Path, "home", return_value=fake_home), mock.patch.object(
                skills.Path, "home", return_value=fake_home
            ):
                self.assertNotEqual(cli.main(["skill", "install"]), 0)
                self.assertFalse((fake_home / ".claude").exists())
                self.assertEqual(diverged.read_text(encoding="utf-8"), "locally edited")
                self.assertEqual(cli.main(["skill", "install", "--force"]), 0)
                self.assertTrue((fake_home / ".claude" / "skills" / BUILDER_SKILL_NAME / "SKILL.md").is_file())

    def test_dangling_symlink_refuses_and_force_replaces_the_link(self):
        from tangram_app import cli, cli_skills, skills

        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            link = fake_home / ".codex" / "prompts" / "tangram-app-builder.md"
            link.parent.mkdir(parents=True)
            link.symlink_to(fake_home / "elsewhere.md")
            with mock.patch.object(cli_skills.Path, "home", return_value=fake_home), mock.patch.object(
                skills.Path, "home", return_value=fake_home
            ):
                self.assertNotEqual(cli.main(["skill", "install"]), 0)
                self.assertFalse((fake_home / ".claude").exists())
                self.assertEqual(cli.main(["skill", "install", "--force"]), 0)
            self.assertFalse(link.is_symlink())
            self.assertFalse((fake_home / "elsewhere.md").exists())
            self.assertEqual(link.read_text(encoding="utf-8"), builder_skill_text())

    def test_nameless_install_honours_a_single_scope(self):
        code, home, payload = self._bare_install(["skill", "install", "--user"])
        self.assertEqual(code, 0)
        for name in ("tangram-app-builder", "tangram-connector-builder"):
            self.assertTrue((home / ".claude" / "skills" / name / "SKILL.md").is_file())
        self.assertFalse((home / ".codex").exists())
        self.assertEqual({row["scope"] for row in payload["data"]["installed"]}, {"user"})


class CodexAgentsTest(unittest.TestCase):
    def _install(self, argv):
        from tangram_app import cli, skills

        directory = tempfile.mkdtemp()
        fake_home = Path(directory)
        with mock.patch.object(cli.Path, "home", return_value=fake_home), mock.patch.object(
            skills.Path, "home", return_value=fake_home
        ):
            code = cli.main(argv)
        return code, fake_home

    def test_codex_install_writes_agents_pointer(self):
        code, home = self._install(["skill", "install-builder", "--codex"])
        self.assertEqual(code, 0)
        agents = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("~/.codex/prompts/tangram-app-builder.md", agents)
        self.assertIn("<!-- >>> tangram skills >>> -->", agents)

    def test_no_agents_flag_skips_the_pointer(self):
        code, home = self._install(["skill", "install-builder", "--codex", "--no-agents"])
        self.assertEqual(code, 0)
        self.assertTrue((home / ".codex" / "prompts" / f"{BUILDER_SKILL_NAME}.md").is_file())
        self.assertFalse((home / ".codex" / "AGENTS.md").exists())

    def test_block_is_idempotent_and_preserves_user_content(self):
        from tangram_app import skills

        directory = tempfile.mkdtemp()
        home = Path(directory) / ".codex"
        (home / "prompts").mkdir(parents=True)
        for name in PACKAGED_SKILLS:
            (home / "prompts" / f"{name}.md").write_text("x", encoding="utf-8")
        agents = home / "AGENTS.md"
        agents.write_text("# my notes\nkeep this\n", encoding="utf-8")

        skills.configure_codex_agents(home)
        first = agents.read_text(encoding="utf-8")
        skills.configure_codex_agents(home)
        second = agents.read_text(encoding="utf-8")

        self.assertEqual(first, second)  # idempotent — no churn on re-run
        self.assertTrue(first.startswith("# my notes\nkeep this\n"))
        self.assertEqual(first.count("<!-- >>> tangram skills >>> -->"), 1)
        # codex_home here is outside $HOME, so the reference is the absolute
        # prompt path (the ~/ form is covered by the CLI tests above).
        for name in PACKAGED_SKILLS:
            self.assertIn(str(home / "prompts" / f"{name}.md"), first)

    def test_lone_marker_is_left_untouched(self):
        from tangram_app import skills

        directory = tempfile.mkdtemp()
        home = Path(directory) / ".codex"
        (home / "prompts").mkdir(parents=True)
        (home / "prompts" / f"{BUILDER_SKILL_NAME}.md").write_text("x", encoding="utf-8")
        agents = home / "AGENTS.md"
        # A begin marker with no matching end (interrupted/hand-edited).
        original = "# notes\n<!-- >>> tangram skills >>> -->\nstray\n"
        agents.write_text(original, encoding="utf-8")

        skills.configure_codex_agents(home)
        # Left verbatim — no second block appended, so no future deletion.
        self.assertEqual(agents.read_text(encoding="utf-8"), original)

    def test_crlf_markers_are_recognized_not_duplicated(self):
        from tangram_app import skills

        directory = tempfile.mkdtemp()
        home = Path(directory) / ".codex"
        (home / "prompts").mkdir(parents=True)
        (home / "prompts" / f"{BUILDER_SKILL_NAME}.md").write_text("x", encoding="utf-8")
        agents = home / "AGENTS.md"
        skills.configure_codex_agents(home)
        # Rewrite the managed block with CRLF endings, as a Windows editor might.
        crlf = agents.read_text(encoding="utf-8").replace("\n", "\r\n")
        agents.write_text(crlf, encoding="utf-8")

        skills.configure_codex_agents(home)
        after = agents.read_text(encoding="utf-8")
        self.assertEqual(after.count("<!-- >>> tangram skills >>> -->"), 1)

    def test_unreadable_agents_is_left_untouched(self):
        from tangram_app import skills

        directory = tempfile.mkdtemp()
        home = Path(directory) / ".codex"
        (home / "prompts").mkdir(parents=True)
        (home / "prompts" / f"{BUILDER_SKILL_NAME}.md").write_text("x", encoding="utf-8")
        agents = home / "AGENTS.md"
        original = b"\xff\xfe not valid utf-8"
        agents.write_bytes(original)

        result = skills.configure_codex_agents(home)
        self.assertIsNone(result)
        self.assertEqual(agents.read_bytes(), original)  # never clobbered


if __name__ == "__main__":
    unittest.main()
