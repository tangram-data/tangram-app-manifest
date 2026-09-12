"""User-level app store: install/list/resolve/uninstall + CLI integration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tangram_app.local_store import (
    LocalStoreError,
    install_app,
    list_installed,
    resolve_installed,
    resolve_target,
    uninstall_app,
    uninstall_app_idle,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-app"


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        previous_agent_home = os.environ.get("TANGRAM_AGENT_HOME")
        os.environ["TANGRAM_AGENT_HOME"] = self._home.name
        self.addCleanup(
            lambda: os.environ.update({"TANGRAM_AGENT_HOME": previous_agent_home})
            if previous_agent_home
            else os.environ.pop("TANGRAM_AGENT_HOME", None)
        )

    def test_uninstall_idle_removes_a_data_free_package(self):
        entry = install_app(str(FIXTURE))
        removed = uninstall_app_idle("com.example/orders")
        self.assertEqual(removed["id"], "com.example/orders")
        self.assertFalse(Path(entry["root"]).exists())

    def test_uninstall_idle_refuses_run_data(self):
        entry = install_app(str(FIXTURE))
        preview = Path(entry["root"]) / ".preview"
        preview.mkdir()
        (preview / "pgdata").mkdir()
        with self.assertRaisesRegex(LocalStoreError, "local run data"):
            uninstall_app_idle("com.example/orders")
        self.assertTrue((preview / "pgdata").is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX flock only")
    def test_uninstall_idle_refuses_a_live_session(self):
        import fcntl

        entry = install_app(str(FIXTURE))
        preview = Path(entry["root"]) / ".preview"
        preview.mkdir()
        with (preview / "runtime.lock").open("a+b") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(LocalStoreError, "live local session"):
                uninstall_app_idle("com.example/orders")
        self.assertTrue(Path(entry["root"]).exists())

    @unittest.skipIf(os.name == "nt", "POSIX flock only")
    def test_uninstall_idle_refuses_when_the_stable_lock_is_held(self):
        import fcntl

        from tangram_app.local_runtime import project_lock_path

        entry = install_app(str(FIXTURE))
        with project_lock_path(Path(entry["root"])).open("a+b") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(LocalStoreError, "live local session"):
                uninstall_app_idle("com.example/orders")
        self.assertTrue(Path(entry["root"]).exists())

    def test_uninstall_idle_leaves_the_stable_lock_outside_the_deleted_tree(self):
        from tangram_app.local_runtime import project_lock_path

        entry = install_app(str(FIXTURE))
        lock_path = project_lock_path(Path(entry["root"]))
        uninstall_app_idle("com.example/orders")
        self.assertFalse(Path(entry["root"]).exists())
        self.assertTrue(lock_path.is_file())

    def test_install_from_directory_then_list_resolve_uninstall(self):
        entry = install_app(str(FIXTURE))
        self.assertEqual(entry["id"], "com.example/orders")
        self.assertEqual(entry["version"], "0.1.0")
        self.assertTrue((Path(entry["root"]) / "manifests" / "app.pkl").is_file())

        listed = list_installed()
        self.assertEqual([e["id"] for e in listed], ["com.example/orders"])
        self.assertEqual(resolve_installed("com.example/orders")["root"], entry["root"])
        self.assertEqual(resolve_installed("orders")["root"], entry["root"])
        self.assertIsNone(resolve_installed("ghost"))

        with self.assertRaises(FileExistsError):
            install_app(str(FIXTURE))
        install_app(str(FIXTURE), force=True)

        uninstall_app("orders")
        self.assertEqual(list_installed(), [])
        with self.assertRaises(LocalStoreError):
            uninstall_app("orders")

    def test_install_from_hub_style_tarball(self):
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / "orders"
            shutil.copytree(FIXTURE, staged)
            archive = Path(directory) / "orders.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(staged, arcname="./orders")
            entry = install_app(str(archive))
        self.assertEqual(entry["id"], "com.example/orders")

    def test_bare_name_ambiguity_is_an_error(self):
        install_app(str(FIXTURE))
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "other"
            shutil.copytree(FIXTURE, other)
            app_pkl = other / "manifests" / "app.pkl"
            app_pkl.write_text(
                app_pkl.read_text(encoding="utf-8").replace(
                    'group = "com.example"', 'group = "org.other"'
                ),
                encoding="utf-8",
            )
            install_app(str(other))
        with self.assertRaises(LocalStoreError):
            resolve_installed("orders")
        self.assertIsNotNone(resolve_installed("org.other/orders"))

    def test_resolve_target_prefers_existing_paths(self):
        entry = install_app(str(FIXTURE))
        self.assertEqual(resolve_target("orders"), Path(entry["root"]))
        self.assertEqual(resolve_target(str(FIXTURE)), FIXTURE)
        self.assertEqual(resolve_target("no-such-thing"), Path("no-such-thing"))

    def test_insecure_and_missing_sources_refuse(self):
        for source in ("http://example.com/app.tar.gz", "/no/such/path"):
            with self.assertRaises(LocalStoreError):
                install_app(source)

    def test_https_redirect_to_http_refuses(self):
        from tangram_app.local_store import _HttpsOnlyRedirect
        from urllib.request import Request

        handler = _HttpsOnlyRedirect()
        with self.assertRaises(LocalStoreError):
            handler.redirect_request(
                Request("https://example.com/app.tar.gz"),
                None, 302, "Found", {}, "http://evil.example.com/app.tar.gz",
            )

    def test_cli_validate_accepts_installed_ref(self):
        install_app(str(FIXTURE))
        completed = subprocess.run(
            [sys.executable, "-m", "tangram_app", "validate", "orders"],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": os.environ["PATH"],
                "TANGRAM_HOME": self._home.name,
                "HOME": os.environ.get("HOME", ""),
            },
        )
        envelope = json.loads(completed.stdout)
        self.assertTrue(envelope["ok"], envelope)
        self.assertTrue(envelope["data"]["valid"])


if __name__ == "__main__":
    unittest.main()
