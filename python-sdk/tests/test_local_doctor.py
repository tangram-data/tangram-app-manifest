"""Doctor: diagnosis shape, pkl auto-install, evaluator fallback."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tangram_app.local_doctor import (
    diagnose,
    find_pkl,
    fix,
    install_pkl,
    managed_pkl_path,
)


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        system_paths = mock.patch("tangram_app.local_doctor._system_pkl_paths", return_value=())
        system_paths.start()
        self.addCleanup(system_paths.stop)

    def test_report_shape_and_required_gating(self):
        report = diagnose()
        names = [check["name"] for check in report["checks"]]
        self.assertEqual(
            names,
            ["python", "python-3.12-backends", "pkl", "postgresql", "node", "tangram-native-cli"],
        )
        for check in report["checks"]:
            if not check["ok"]:
                self.assertTrue(check["hint"], check["name"])
        required = {c["name"] for c in report["checks"] if c["required"]}
        self.assertEqual(required, {"python", "pkl"})

    def test_missing_pkl_flags_with_fix_hint(self):
        with mock.patch("tangram_app.local_doctor.shutil.which", return_value=None):
            report = diagnose()
        pkl = next(c for c in report["checks"] if c["name"] == "pkl")
        self.assertFalse(pkl["ok"])
        self.assertIn("doctor --fix", pkl["hint"])
        self.assertFalse(report["ok"])

    def test_find_pkl_prefers_path_then_managed(self):
        managed = managed_pkl_path()
        managed.parent.mkdir(parents=True)
        managed.write_text("#!/bin/sh\nexit 0\n")
        managed.chmod(0o755)
        with mock.patch("tangram_app.local_doctor.shutil.which", return_value=None):
            self.assertEqual(find_pkl(), str(managed))
        with mock.patch("tangram_app.local_doctor.shutil.which", return_value="/usr/bin/pkl"):
            self.assertEqual(find_pkl(), "/usr/bin/pkl")

    def test_fix_is_noop_when_pkl_present(self):
        with mock.patch("tangram_app.local_doctor.shutil.which", return_value="/usr/bin/pkl"):
            self.assertEqual(fix(), [])

    def test_gui_path_uses_system_pkl_for_doctor_and_evaluator(self):
        from tangram_app.pkl import PklEvaluator

        installed = Path(self._home.name) / "homebrew/bin/pkl"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/bin/sh\necho '42'\n")
        installed.chmod(0o755)
        module = Path(self._home.name) / "test.pkl"
        module.write_text("value = 42\n")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}), \
                mock.patch("tangram_app.local_doctor._system_pkl_paths", return_value=(installed,)), \
                mock.patch("tangram_app.local_doctor.install_pkl") as install:
            report = diagnose()
            pkl = next(c for c in report["checks"] if c["name"] == "pkl")
            self.assertTrue(pkl["ok"])
            self.assertEqual(pkl["detail"], str(installed))
            self.assertEqual(fix(), [])
            install.assert_not_called()
            evaluator = PklEvaluator()
            self.assertEqual(evaluator.executable, str(installed))
            self.assertEqual(evaluator.evaluate(module, expression=None,
                                               root_dir=module.parent, project_dir=None), 42)
            managed = managed_pkl_path()
            managed.parent.mkdir(parents=True)
            managed.write_text("#!/bin/sh\nexit 0\n")
            managed.chmod(0o755)
            self.assertEqual(find_pkl(), str(managed))

    def test_system_fallback_rejects_nonexecutable_and_directory(self):
        candidate = Path(self._home.name) / "pkl"
        candidate.write_text("not executable")
        candidate.chmod(0o644)
        with mock.patch("tangram_app.local_doctor.shutil.which", return_value=None), \
                mock.patch("tangram_app.local_doctor._system_pkl_paths", return_value=(candidate, candidate.parent)):
            self.assertIsNone(find_pkl())

    def test_explicit_pkl_path_does_not_fall_back(self):
        from tangram_app.pkl import PklEvaluator
        from tangram_app.errors import PklNotFoundError

        with mock.patch("tangram_app.local_doctor.find_pkl") as fallback:
            with self.assertRaises(PklNotFoundError):
                PklEvaluator(Path(self._home.name) / "missing-pkl")
            fallback.assert_not_called()

    def test_bad_download_never_clobbers_working_binary(self):
        from tangram_app.local_doctor import DoctorError

        managed = managed_pkl_path()
        managed.parent.mkdir(parents=True)
        managed.write_text("#!/bin/sh\necho Pkl 0.0.0\n")
        managed.chmod(0o755)

        def fake_fetch(url, timeout):
            import io

            return mock.MagicMock(
                __enter__=lambda s: io.BytesIO(b"\x7fELFgarbage"),
                __exit__=lambda s, *a: False,
            )

        with mock.patch("tangram_app.local_doctor.urllib.request.urlopen", fake_fetch):
            with self.assertRaises(DoctorError):
                install_pkl()
        self.assertIn("Pkl 0.0.0", managed.read_text())  # untouched
        self.assertFalse(managed.with_suffix(".download").exists())


class InstallPklTest(unittest.TestCase):
    """Real download into a sandbox home — network required."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)

    def test_installs_a_working_binary(self):
        try:
            target = install_pkl()
        except Exception as error:  # offline CI etc.
            self.skipTest(f"pkl download unavailable: {error}")
        completed = subprocess.run(
            [str(target), "--version"], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Pkl", completed.stdout)

    def test_evaluator_falls_back_to_managed_copy(self):
        from tangram_app.pkl import PklEvaluator

        managed = managed_pkl_path()
        managed.parent.mkdir(parents=True)
        managed.write_text("#!/bin/sh\nexit 0\n")
        managed.chmod(0o755)
        with mock.patch("tangram_app.pkl.shutil.which", return_value=None):
            evaluator = PklEvaluator()
        self.assertEqual(evaluator.executable, str(managed))


if __name__ == "__main__":
    unittest.main()
