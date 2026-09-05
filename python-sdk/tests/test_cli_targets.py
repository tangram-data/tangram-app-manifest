"""v3 CLI consistency: version envelope, selector grammar, consent gates,
normalized app views, --upgrade unification."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tangram_app import cli
from tangram_app.cli_targets import TargetError, resolve_os_selector
from tangram_app.local_store import install_app

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-app"


def _args(**overrides):
    base = dict(
        source=str(FIXTURE), force=False, workspace=None, instance=None,
        os_url=None, token=None, dry_run=False, upgrade=False, yes=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, json.loads(out.getvalue())


class VersionEnvelopeTest(unittest.TestCase):
    def test_version_is_a_standard_envelope(self):
        code, envelope = _run_cli("--version")
        self.assertEqual(code, 0)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["protocol"], "1")
        self.assertTrue(envelope["data"]["version"])
        code, envelope = _run_cli("--version", "junk")
        self.assertNotEqual(code, 0)  # extra tokens are not silently ignored
        self.assertFalse(envelope["ok"])


class SelectorGrammarTest(unittest.TestCase):
    def test_valid_forms(self):
        qualified = resolve_os_selector(_args(workspace="prod/analytics"))
        self.assertEqual(qualified, {"instance": "prod", "workspace": "analytics", "ambient": False})
        explicit = resolve_os_selector(_args(workspace="ws", instance="dev"))
        self.assertFalse(explicit["ambient"])
        url_form = resolve_os_selector(_args(workspace="ws", os_url="https://x", token="t"))
        self.assertEqual(url_form["url"], "https://x")
        ambient = resolve_os_selector(_args(workspace="ws"))
        self.assertTrue(ambient["ambient"])

    def test_conflicts_refuse_never_precedence(self):
        conflicts = [
            _args(workspace="prod/ws", instance="dev"),
            _args(workspace="prod/ws", os_url="https://x", token="t"),
            _args(workspace="ws", os_url="https://x"),  # token missing
            _args(workspace="ws", instance="dev", os_url="https://x", token="t"),
            _args(workspace="/ws"),
        ]
        for args in conflicts:
            with self.assertRaises(TargetError, msg=vars(args)):
                resolve_os_selector(args)


class _FakeOs(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = json.dumps({"accepted": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class ConsentAndDispatchTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOs)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        home = Path(self._home.name)
        url = f"http://127.0.0.1:{self.server.server_port}"
        (home / ".credentials").write_text(json.dumps(
            [{"instance": "dev", "url": url, "token": "tok"},
             {"instance": "prod", "url": url, "token": "tok2"}]))
        (home / ".HEAD").write_text(json.dumps({"instance": "dev", "url": url}))

    def test_non_tty_ambient_refuses_without_yes(self):
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--workspace", "demo")
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", envelope["error"]["message"])
        self.assertIn("dev", envelope["error"]["message"])  # lists configured instances

    def test_non_tty_ambient_with_yes_and_explicit_selector_proceed(self):
        code, envelope = _run_cli(
            "app", "install", str(FIXTURE), "--workspace", "demo", "--yes")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["deployed"]["instance"], "dev")
        code, envelope = _run_cli(
            "app", "install", str(FIXTURE), "--workspace", "prod/demo")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["deployed"]["instance"], "prod")

    def test_uninstall_requires_yes_when_non_interactive(self):
        install_app(str(FIXTURE))
        code, envelope = _run_cli("app", "uninstall", "orders")
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", envelope["error"]["message"])
        code, envelope = _run_cli("app", "uninstall", "orders", "--yes")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["uninstalled"]["context"], "machine")

    def test_normalized_list_and_get(self):
        install_app(str(FIXTURE))
        code, envelope = _run_cli("app", "list")
        row = envelope["data"]["apps"][0]
        self.assertEqual(
            set(row), {"id", "version", "context", "state", "detail"})
        self.assertEqual(row["context"], "machine")
        self.assertEqual(row["state"], "installed")
        code, envelope = _run_cli("app", "get", "orders")
        self.assertEqual(envelope["data"]["app"]["id"], "com.example/orders")
        code, envelope = _run_cli("app", "get", "ghost")
        self.assertNotEqual(code, 0)

    def test_upgrade_replaces_and_force_warns_deprecated(self):
        install_app(str(FIXTURE))
        code, envelope = _run_cli("app", "install", str(FIXTURE))
        self.assertNotEqual(code, 0)  # exists, no replace flag
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--upgrade")
        self.assertEqual(code, 0, envelope)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, envelope = _run_cli("app", "install", str(FIXTURE), "--force")
        self.assertEqual(code, 0, envelope)
        self.assertIn("deprecated", err.getvalue())


if __name__ == "__main__":
    unittest.main()
