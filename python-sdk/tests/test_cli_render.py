"""TTY rendering: humans get text, every non-tty consumer gets the envelope."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tangram_app import cli
from tangram_app.cli_render import render_data, render_error


def _run(argv, tty):
    out, err = io.StringIO(), io.StringIO()
    out.isatty = lambda: tty
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class RenderModeTest(unittest.TestCase):
    def test_non_tty_keeps_the_envelope(self):
        code, out, _ = _run(["doctor"], tty=False)
        envelope = json.loads(out)
        self.assertIn("checks", envelope["data"])

    def test_tty_renders_text(self):
        code, out, _ = _run(["doctor"], tty=True)
        with self.assertRaises(ValueError):
            json.loads(out)  # not an envelope
        self.assertIn("pkl", out)
        self.assertIn("environment", out)

    def test_json_flag_forces_envelope_on_tty(self):
        code, out, _ = _run(["--json", "doctor"], tty=True)
        envelope = json.loads(out)
        self.assertTrue(envelope["ok"])

    def test_tty_errors_go_to_stderr_as_text(self):
        code, out, err = _run(["validate", "/no/such/package"], tty=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("error [", err)

    def test_help_renders_plain_on_tty(self):
        code, out, _ = _run(["--help"], tty=True)
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)
        with self.assertRaises(ValueError):
            json.loads(out)


class BrokenPipeTest(unittest.TestCase):
    def test_closed_pipe_exits_141_without_traceback(self):
        import subprocess
        import sys as _sys
        from pathlib import Path

        completed = subprocess.run(
            [
                _sys.executable,
                "-c",
                "import sys; sys.argv=['tangram-app','doctor']; "
                "from tangram_app.cli import main; "
                "sys.stdout.close = lambda: None\n"
                "import io, os\n"
                "read_end, write_end = os.pipe()\n"
                "os.close(read_end)\n"  # reader gone: writes will EPIPE
                "os.dup2(write_end, 1)\n"
                "raise SystemExit(main(['doctor']))",
            ],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
            },
        )
        self.assertEqual(completed.returncode, 141, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("BrokenPipeError", completed.stderr)

    def test_skill_runner_swallows_broken_pipe(self):
        from tangram_app import cli

        with mock.patch.object(cli, "verify_skill", side_effect=BrokenPipeError), \
             mock.patch.object(cli, "_swallow_broken_pipe"):  # dup2 would hijack pytest's fd 1
            code = cli.skill_runner_main("/tmp/none", ["inspect"])
        self.assertEqual(code, 141)


class RendererUnitTest(unittest.TestCase):
    def test_doctor_layout(self):
        text = render_data(
            "doctor",
            {
                "ok": False,
                "applied": [{"check": "pkl", "action": "installed 0.25.3"}],
                "checks": [
                    {"name": "python", "ok": True, "required": True, "detail": "3.12.7", "hint": None},
                    {"name": "pkl", "ok": False, "required": True, "detail": None, "hint": "run --fix"},
                ],
            },
        )
        self.assertIn("fixed: pkl", text)
        self.assertIn("!! pkl", text)
        self.assertIn("hint: run --fix", text)
        self.assertIn("environment NOT ready", text)

    def test_apps_actions_and_error_layouts(self):
        apps = render_data("app", {"apps": [
            {"id": "com.example/orders", "version": "0.1.0", "context": "machine", "state": "installed"}
        ]})
        self.assertIn("com.example/orders  0.1.0  [machine: installed]", apps)
        self.assertIn("no apps installed", render_data("app", {"apps": []}))
        actions = render_data("actions", {"app": "com.example/orders", "actions": [
            {"ref": "Order.List", "effect": "Stateless", "requiresConfirmation": False, "doc": "List"}
        ]})
        self.assertIn("Order.List  [Stateless]", actions)
        self.assertEqual(render_error({"code": "x", "message": "m"}), "error [x]: m")


if __name__ == "__main__":
    unittest.main()
