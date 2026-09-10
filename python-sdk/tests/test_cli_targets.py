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
from tangram_app.cli_targets import TargetError, resolve_os_selector, GLOBAL_APP_WORKSPACE
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
    def setUp(self):
        # Ambient forms read the context — isolate from the real ~/.tangram.
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)
        (Path(self._home.name) / ".HEAD").write_text(
            json.dumps({"instance": "dev", "url": "os.example.com"})
        )

    def test_valid_forms(self):
        qualified = resolve_os_selector(_args(workspace="prod/analytics"))
        self.assertEqual(qualified, {"instance": "prod", "workspace": "analytics", "ambient": False})
        explicit = resolve_os_selector(_args(workspace="ws", instance="dev"))
        self.assertFalse(explicit["ambient"])
        url_form = resolve_os_selector(_args(workspace="ws", os_url="https://x", token="t"))
        self.assertEqual(url_form["url"], "https://x")
        ambient = resolve_os_selector(_args(workspace="ws"))
        self.assertTrue(ambient["ambient"])
        self.assertEqual(ambient["instance"], "dev")

    def test_global_scope_ignores_ambient_workspace_but_rejects_explicit_workspace(self):
        with mock.patch.dict(os.environ, {"TANGRAM_WORKSPACE": "analytics"}):
            selector = resolve_os_selector(_args(global_install=True))
        self.assertEqual(selector["instance"], "dev")
        self.assertEqual(selector["workspace"], GLOBAL_APP_WORKSPACE)
        self.assertEqual(selector["scope"], "global")
        for workspace in ("analytics", "prod/analytics", "", "  "):
            with self.assertRaises(TargetError):
                resolve_os_selector(_args(global_install=True, workspace=workspace))

    def test_global_explicit_instance_and_url_selectors(self):
        for kwargs in ({"instance": "prod"}, {"os_url": "https://os.example.com", "token": "fake"}):
            selector = resolve_os_selector(_args(global_install=True, **kwargs))
            self.assertFalse(selector["ambient"])
            self.assertEqual(selector["workspace"], GLOBAL_APP_WORKSPACE)
        for kwargs in ({"instance": "local"}, {"instance": ""}, {"os_url": "https://x"},
                       {"instance": "prod", "os_url": "https://x", "token": "t"}):
            with self.assertRaises(TargetError):
                resolve_os_selector(_args(global_install=True, **kwargs))

    def test_global_requires_an_os_instance(self):
        with mock.patch.dict(os.environ, {"TANGRAM_INSTANCE": "local"}):
            with self.assertRaises(TargetError):
                resolve_os_selector(_args(global_install=True))
            self.assertEqual(resolve_os_selector(_args(global_install=True, instance="prod"))["instance"], "prod")

    def test_conflicts_refuse_never_precedence(self):
        conflicts = [
            _args(workspace="prod/ws", instance="dev"),
            _args(workspace="prod/ws", os_url="https://x", token="t"),
            _args(workspace="ws", os_url="https://x"),  # token missing
            _args(workspace="ws", instance="dev", os_url="https://x", token="t"),
            _args(workspace="/ws"),
            _args(workspace="  "),  # explicitly empty is never ambient
        ]
        for args in conflicts:
            with self.assertRaises(TargetError, msg=vars(args)):
                resolve_os_selector(args)

    def test_machine_context_refuses_ambient_os_selector(self):
        # Downstream must never re-read ambient state to fill the instance.
        (Path(self._home.name) / ".HEAD").unlink()
        with self.assertRaises(TargetError) as caught:
            resolve_os_selector(_args(workspace="ws"))
        self.assertIn("machine", str(caught.exception))


class _FakeOs(BaseHTTPRequestHandler):
    requests = []
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.requests.append((self.path, json.loads(self.rfile.read(length))))
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
        previous_agent_home = os.environ.get("TANGRAM_AGENT_HOME")
        os.environ["TANGRAM_AGENT_HOME"] = self._home.name
        self.addCleanup(
            lambda: os.environ.update({"TANGRAM_AGENT_HOME": previous_agent_home})
            if previous_agent_home
            else os.environ.pop("TANGRAM_AGENT_HOME", None)
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOs)
        _FakeOs.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        home = Path(self._home.name)
        url = f"http://127.0.0.1:{self.server.server_port}"
        (home / ".credentials").write_text(json.dumps(
            [{"instance": "dev", "url": url, "token": "tok"},
             {"instance": "prod", "url": url, "token": "tok2"}]))
        (home / ".HEAD").write_text(json.dumps({"instance": "dev", "url": url}))

    def _machine_context(self):
        (Path(self._home.name) / ".HEAD").unlink()

    def _os_context(self, workspace: str | None = "demo"):
        head = {"instance": "dev", "url": f"http://127.0.0.1:{self.server.server_port}"}
        if workspace:
            head["workspace"] = workspace
        (Path(self._home.name) / ".HEAD").write_text(json.dumps(head))

    def test_global_install_posts_to_shared_scope_and_reports_it(self):
        self._os_context("analytics")
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--global", "--dry-run", "--yes")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["deployed"]["scope"], "global")
        path, body = _FakeOs.requests[-1]
        self.assertEqual(path, "/api/core/v1/workspaces/tangram_os_shared/apps:install")
        self.assertTrue(body["dryRun"])
        self.assertEqual(body["source"]["kind"], "local-package")

    def test_global_install_keeps_ambient_consent_gate(self):
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--global")
        self.assertNotEqual(code, 0)
        self.assertIn("shared across all workspaces", envelope["error"]["message"])
        self.assertEqual(_FakeOs.requests, [])
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--global", "--instance", "prod")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["deployed"]["instance"], "prod")

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
        self._machine_context()
        install_app(str(FIXTURE))
        code, envelope = _run_cli("app", "uninstall", "orders")
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", envelope["error"]["message"])
        code, envelope = _run_cli("app", "uninstall", "orders", "--yes")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["uninstalled"]["context"], "local")

    def test_normalized_list_and_get(self):
        self._machine_context()
        install_app(str(FIXTURE))
        code, envelope = _run_cli("app", "list")
        row = envelope["data"]["apps"][0]
        self.assertEqual(
            set(row), {"id", "version", "context", "state", "detail"})
        self.assertEqual(row["context"], "local")
        self.assertEqual(row["state"], "installed")
        code, envelope = _run_cli("app", "get", "orders")
        self.assertEqual(envelope["data"]["app"]["id"], "com.example/orders")
        code, envelope = _run_cli("app", "get", "ghost")
        self.assertNotEqual(code, 0)

    def test_upgrade_replaces_and_force_warns_deprecated(self):
        self._machine_context()
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

    def test_selector_free_install_follows_the_os_context(self):
        self._os_context("demo")
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--yes")
        self.assertEqual(code, 0, envelope)
        deployed = envelope["data"]["deployed"]
        self.assertEqual(deployed["instance"], "dev")
        self.assertEqual(deployed["workspace"], "demo")

    def test_selector_free_install_dry_run_previews_in_os_context(self):
        self._os_context("demo")
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--dry-run", "--yes")
        self.assertEqual(code, 0, envelope)
        self.assertTrue(envelope["data"]["deployed"]["dryRun"])

    def test_explicitly_empty_workspace_refuses_never_ambient(self):
        # An unset shell variable (--workspace "$WS") must not deploy to the
        # ambient workspace or fall through to the machine lane.
        self._os_context("demo")
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--workspace", "", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("must not be empty", envelope["error"]["message"])

    def test_selector_free_install_instance_only_context_refuses(self):
        self._os_context(workspace=None)
        code, envelope = _run_cli("app", "install", str(FIXTURE), "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("no workspace", envelope["error"]["message"])

    def test_machine_store_verbs_refuse_under_an_os_context(self):
        self._machine_context()
        install_app(str(FIXTURE))
        self._os_context("demo")
        for argv in (
            ("app", "list"),
            ("app", "get", "orders"),
            ("app", "uninstall", "orders", "--yes"),
        ):
            code, envelope = _run_cli(*argv)
            self.assertNotEqual(code, 0, argv)
            message = envelope["error"]["message"]
            self.assertIn("dev/demo", message, argv)
            self.assertIn("tangram use local", message, argv)


if __name__ == "__main__":
    unittest.main()
