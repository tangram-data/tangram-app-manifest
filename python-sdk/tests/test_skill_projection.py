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
        previous_agent_home = os.environ.get("TANGRAM_AGENT_HOME")
        os.environ["TANGRAM_AGENT_HOME"] = str(self.home)
        self.addCleanup(
            lambda: os.environ.update({"TANGRAM_AGENT_HOME": previous_agent_home})
            if previous_agent_home
            else os.environ.pop("TANGRAM_AGENT_HOME", None)
        )

    def _skill(self, agent: str, name: str) -> Path:
        return self.home / f".{agent}" / "skills" / name / "SKILL.md"

    def test_install_projects_by_default(self):
        code, envelope = _run_cli("app", "install", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("projection", envelope["data"]["installed"]["detail"])
        for agent in ("claude", "codex"):
            text = self._skill(agent, "tangram-orders").read_text(encoding="utf-8")
            self.assertIn("managed-by: tangram-app-sdk", text)
            self.assertIn("--package-digest sha256:", text)
            self.assertIn("inspect com.example/orders --action", text)
            # Freeform action doc is pending, not projected.
            self.assertNotIn("List orders", text)

    def test_no_skills_flag_and_disable_stay_off(self):
        code, _ = _run_cli("app", "install", str(FIXTURE), "--no-skills")
        self.assertEqual(code, 0)
        self.assertFalse(self._skill("claude", "tangram-orders").exists())
        code, envelope = _run_cli("skill", "sync", "--disable")
        self.assertEqual(code, 0)
        code, _ = _run_cli("app", "install", str(FIXTURE), "--upgrade")
        self.assertEqual(code, 0)
        self.assertFalse(self._skill("claude", "tangram-orders").exists())
        code, envelope = _run_cli("skill", "sync")
        self.assertFalse(envelope["data"]["enabled"])

    def test_context_switch_removes_other_context_keeps_active(self):
        _run_cli("app", "install", str(FIXTURE))
        th = Path(os.environ["TANGRAM_HOME"])
        th.mkdir(parents=True, exist_ok=True)

        def fake_ws_skill(name: str, context: str) -> Path:
            skill = self._skill("claude", name)
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\n"
                f"name: {name}\n"
                'description: "ws"\n'
                "metadata:\n"
                "  managed-by: tangram-app-sdk\n"
                "  tangram-app: ws.group/app\n"
                f"  tangram-context: {context}\n"
                "  package-digest: sha256:0\n"
                "---\n",
                encoding="utf-8",
            )
            return skill

        dev = fake_ws_skill("tangram-dev-app", "instance:dev")
        prod = fake_ws_skill("tangram-prod-app", "instance:prod")
        (th / ".HEAD").write_text(json.dumps({"instance": "dev"}), encoding="utf-8")
        code, envelope = _run_cli("skill", "sync")
        self.assertEqual(code, 0)
        self.assertEqual(envelope["data"]["context"], "instance:dev")
        self.assertTrue(dev.is_file())  # active context: kept
        self.assertFalse(prod.exists())  # other context: removed
        self.assertIn("claude:tangram-dev-app", envelope["data"]["keptActiveContext"])
        # Machine skills are context-independent.
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())
        # Switching away removes the dev skill too.
        (th / ".HEAD").write_text(json.dumps({"instance": "prod"}), encoding="utf-8")
        code, envelope = _run_cli("skill", "sync")
        self.assertFalse(dev.exists())

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

    def test_generated_skill_pins_local_for_discovery_and_execution(self):
        # Machine-store skills must be context-immune: discovery and
        # execution BOTH pin --local, or an OS context would show a
        # different app's catalog than the one invoked.
        install_app(str(FIXTURE))
        _run_cli("skill", "sync", "--enable")
        text = self._skill("claude", "tangram-orders").read_text(encoding="utf-8")
        self.assertIn("`tangram-app actions com.example/orders --local`", text)
        self.assertIn("--local --input-json", text)

    def test_rebuild_backfills_missing_projection(self):
        entry = install_app(str(FIXTURE))
        marker = Path(entry["root"]) / ".install.json"
        record = json.loads(marker.read_text(encoding="utf-8"))
        del record["projection"]
        marker.write_text(json.dumps(record), encoding="utf-8")
        _run_cli("skill", "sync", "--enable")
        code, envelope = _run_cli("skill", "sync")
        skipped = envelope["data"]["skippedNoProjection"]
        self.assertEqual([row["app"] for row in skipped], ["com.example/orders"])
        code, envelope = _run_cli("skill", "sync", "--rebuild")
        self.assertEqual(envelope["data"]["rebuilt"], ["com.example/orders"])
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())

    def test_marker_mention_in_body_is_not_ownership(self):
        foreign = self._skill("claude", "tangram-orders")
        foreign.parent.mkdir(parents=True)
        foreign.write_text(
            "# my notes\n\nmentions managed-by: tangram-app-sdk in prose\n", encoding="utf-8"
        )
        _run_cli("app", "install", str(FIXTURE))
        code, envelope = _run_cli("skill", "sync", "--enable")
        self.assertTrue(any("not managed" in c for c in envelope["data"]["conflicts"]))
        self.assertIn("my notes", foreign.read_text(encoding="utf-8"))

    def test_corrupt_install_record_keeps_skill_and_name(self):
        entry = install_app(str(FIXTURE))
        _run_cli("skill", "sync", "--enable")
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())
        marker = Path(entry["root"]) / ".install.json"
        marker.write_text("{corrupt", encoding="utf-8")
        code, envelope = _run_cli("skill", "sync")
        self.assertEqual(code, 0)
        self.assertEqual(envelope["data"]["removed"], [])
        self.assertTrue(self._skill("claude", "tangram-orders").is_file())
        config = json.loads((Path(os.environ["TANGRAM_HOME"]) / "config.json").read_text())
        self.assertIn("com.example/orders", config["skillProjection"]["names"])

    def test_unsafe_projection_record_is_skipped_not_rendered(self):
        entry = install_app(str(FIXTURE))
        marker = Path(entry["root"]) / ".install.json"
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["projection"]["actions"][0]["ref"] = "Order.List`\n---\ninjected"
        marker.write_text(json.dumps(record), encoding="utf-8")
        code, envelope = _run_cli("skill", "sync", "--enable")
        self.assertEqual(code, 0)
        skipped = envelope["data"]["skippedNoProjection"]
        self.assertTrue(any("unsafe" in row["problem"] for row in skipped))
        self.assertFalse(self._skill("claude", "tangram-orders").exists())

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
