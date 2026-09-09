"""Installed apps projected as agent skills (skill sync)."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tangram_app import cli, skill_projection
from tangram_app.local_store import install_app

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-app"


def _run_cli(*argv):
    stdout = StringIO()
    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        code = cli.main(list(argv))
    return code, json.loads(stdout.getvalue())


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name)
        os.environ["TANGRAM_HOME"] = str(self.home / "th")
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        patcher = mock.patch.object(skill_projection.Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _skill(self, agent: str, name: str) -> Path:
        return self.home / f".{agent}" / "skills" / name / "SKILL.md"

    def test_off_by_default_and_enable_projects(self):
        code, envelope = _run_cli("app", "install", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("projection", envelope["data"]["installed"]["detail"])
        self.assertFalse(self._skill("claude", "tangram-orders").exists())

        code, envelope = _run_cli("skill", "sync", "--enable")
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(envelope["data"]["added"]),
            ["claude:tangram-orders", "codex:tangram-orders"],
        )
        for agent in ("claude", "codex"):
            text = self._skill(agent, "tangram-orders").read_text(encoding="utf-8")
            self.assertIn("managed-by: tangram-app-sdk", text)
            self.assertIn("--package-digest sha256:", text)
            self.assertIn("inspect com.example/orders --action", text)
            # Freeform action doc is pending, not projected.
            self.assertNotIn("List orders", text)

    def test_freeform_projects_only_after_digest_approval(self):
        _run_cli("app", "install", str(FIXTURE))
        _run_cli("skill", "sync", "--enable")
        code, envelope = _run_cli("skill", "sync", "--status")
        pending = envelope["data"]["pendingFreeform"]
        self.assertEqual(len(pending), 1)
        code, envelope = _run_cli("skill", "sync", "--approve", pending[0]["digest"])
        self.assertEqual(code, 0)
        text = self._skill("claude", "tangram-orders").read_text(encoding="utf-8")
        self.assertIn("app says: List orders", text)
        code, _ = _run_cli("skill", "sync", "--approve", "nothex")
        self.assertNotEqual(code, 0)

    def test_uninstall_removes_and_disable_clears(self):
        _run_cli("app", "install", str(FIXTURE))
        _run_cli("skill", "sync", "--enable")
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())
        code, _ = _run_cli("app", "uninstall", "orders", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self._skill("claude", "tangram-orders").exists())
        self.assertFalse(self._skill("codex", "tangram-orders").exists())

        _run_cli("app", "install", str(FIXTURE))
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())
        code, envelope = _run_cli("skill", "sync", "--disable")
        self.assertEqual(code, 0)
        self.assertFalse(self._skill("claude", "tangram-orders").exists())

    def test_never_touches_foreign_dirs(self):
        foreign = self._skill("claude", "tangram-orders")
        foreign.parent.mkdir(parents=True)
        foreign.write_text("# user-authored\n", encoding="utf-8")
        _run_cli("app", "install", str(FIXTURE))
        code, envelope = _run_cli("skill", "sync", "--enable")
        self.assertEqual(code, 0)
        self.assertTrue(any("not managed" in c for c in envelope["data"]["conflicts"]))
        self.assertEqual(foreign.read_text(encoding="utf-8"), "# user-authored\n")
        # The codex side has no conflict and converges.
        self.assertTrue(self._skill("codex", "tangram-orders").is_file())

    def test_extra_files_block_removal(self):
        _run_cli("app", "install", str(FIXTURE))
        _run_cli("skill", "sync", "--enable")
        managed = self._skill("claude", "tangram-orders").parent
        (managed / "notes.txt").write_text("mine", encoding="utf-8")
        _run_cli("app", "uninstall", "orders", "--yes")
        code, envelope = _run_cli("skill", "sync")
        self.assertTrue(any("unmanaged files" in c for c in envelope["data"]["conflicts"]))
        self.assertTrue((managed / "notes.txt").is_file())

    def test_name_allocation_is_persisted_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as scratch:
            other = Path(scratch) / "pkg"
            shutil.copytree(FIXTURE, other)
            app_pkl = other / "manifests" / "app.pkl"
            app_pkl.write_text(
                app_pkl.read_text(encoding="utf-8").replace("com.example", "org.other"),
                encoding="utf-8",
            )
            code, envelope = _run_cli("app", "install", str(FIXTURE))
            self.assertEqual(code, 0, envelope)
            code, envelope = _run_cli("app", "install", str(other))
            self.assertEqual(code, 0, envelope)
        _run_cli("skill", "sync", "--enable")
        config = json.loads((Path(os.environ["TANGRAM_HOME"]) / "config.json").read_text())
        names = config["skillProjection"]["names"]
        self.assertEqual(names["com.example/orders"], "tangram-orders")
        self.assertTrue(names["org.other/orders"].startswith("tangram-orders-"))
        # Removing the hashed newcomer never renames the original.
        _run_cli("app", "uninstall", "org.other/orders", "--yes")
        config = json.loads((Path(os.environ["TANGRAM_HOME"]) / "config.json").read_text())
        self.assertEqual(
            config["skillProjection"]["names"], {"com.example/orders": "tangram-orders"}
        )

    def test_rebuild_backfills_missing_projection(self):
        entry = install_app(str(FIXTURE))
        marker = Path(entry["root"]) / ".install.json"
        record = json.loads(marker.read_text(encoding="utf-8"))
        del record["projection"]
        marker.write_text(json.dumps(record), encoding="utf-8")
        _run_cli("skill", "sync", "--enable")
        code, envelope = _run_cli("skill", "sync")
        self.assertEqual(envelope["data"]["skippedNoProjection"], ["com.example/orders"])
        code, envelope = _run_cli("skill", "sync", "--rebuild")
        self.assertEqual(envelope["data"]["rebuilt"], ["com.example/orders"])
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())

    def test_package_digest_guard(self):
        entry = install_app(str(FIXTURE))
        digest = entry["projection"]["packageDigest"]
        code, envelope = _run_cli(
            "inspect", "orders", "--action", "Order.List", "--package-digest", "sha256:stale"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("skill sync", envelope["error"]["message"])
        code, envelope = _run_cli(
            "inspect", "orders", "--action", "Order.List", "--package-digest", digest
        )
        self.assertEqual(code, 0, envelope)


if __name__ == "__main__":
    unittest.main()
