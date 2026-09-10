"""Workspace lane for `actions` / `call` (S3): selector routing, short-ref
resolution against the server catalog, mutation consent, wire shape."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from tangram_app import cli

CATALOG = {
    "app": "todo",
    "schemaVersion": 3,
    "actions": [
        {
            "resourceType": "Todo",
            "action": "List",
            "effect": "Stateless",
            "requiresConfirmation": False,
        },
        {
            "resourceType": "Todo",
            "action": "Write",
            "effect": "Reversible",
            "requiresConfirmation": False,
        },
    ],
}


class _FakeOs(BaseHTTPRequestHandler):
    performed: list[dict] = []

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/apps/todo/actions"):
            self._reply(CATALOG)
        elif self.path.endswith("/api/core/v1/workspaces"):
            self._reply([{"name": "demo"}, {"metadata": {"name": "analytics"}}])
        else:
            self._reply({"message": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/actions/perform"):
            _FakeOs.performed.append(
                {"path": self.path, "auth": self.headers.get("Authorization"), "body": payload}
            )
            self._reply({"result": {"items": []}})
        else:
            self._reply({"message": "not found"}, 404)

    def log_message(self, *args):
        pass


def _run_cli(*argv):
    stdout = StringIO()
    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        code = cli.main(list(argv))
    return code, json.loads(stdout.getvalue())


class OsActionsLaneTest(unittest.TestCase):
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
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        _FakeOs.performed = []
        home = Path(self._home.name)
        url = f"http://127.0.0.1:{self.server.server_port}"
        home.mkdir(exist_ok=True)
        (home / ".credentials").write_text(
            json.dumps([{"instance": "dev", "url": url, "token": "tok"}]), encoding="utf-8"
        )
        (home / ".HEAD").write_text(json.dumps({"instance": "dev", "url": url}), encoding="utf-8")

    def test_actions_lists_the_server_catalog(self):
        code, envelope = _run_cli("actions", "todo", "--workspace", "demo")
        self.assertEqual(code, 0, envelope)
        data = envelope["data"]
        self.assertEqual(data["workspace"], "demo")
        self.assertEqual(data["app"], "todo")
        self.assertEqual(len(data["actions"]), 2)

    def test_call_resolves_short_ref_and_performs(self):
        code, envelope = _run_cli(
            "call", "todo", "List", "--workspace", "demo", "--input-json", "{}"
        )
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["action"], "Todo.List")
        self.assertEqual(len(_FakeOs.performed), 1)
        performed = _FakeOs.performed[0]
        self.assertEqual(performed["auth"], "Bearer tok")
        self.assertEqual(performed["body"]["resource_type"], "Todo")
        self.assertEqual(performed["body"]["action"], "List")

    def test_mutation_needs_explicit_consent(self):
        code, envelope = _run_cli(
            "call", "todo", "Write", "--workspace", "demo", "--input-json", "{}"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--allow-mutation", envelope["error"]["message"])
        self.assertEqual(_FakeOs.performed, [])
        code, envelope = _run_cli(
            "call", "todo", "Write", "--workspace", "demo", "--input-json", "{}", "--allow-mutation"
        )
        self.assertEqual(code, 0, envelope)

    def test_unknown_and_lane_conflicts_refuse(self):
        code, envelope = _run_cli(
            "call", "todo", "Nope", "--workspace", "demo", "--input-json", "{}"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("declared", envelope["error"]["message"])
        code, envelope = _run_cli(
            "call", "todo", "List", "--workspace", "demo", "--local", "--input-json", "{}"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("OS lane", envelope["error"]["message"])
        code, envelope = _run_cli(
            "call", "todo", "List", "--workspace", "demo", "--input-json", "{}", "--confirm"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("confirmation", envelope["error"]["message"])


class AmbientWorkspaceContextTest(OsActionsLaneTest):
    def _set_head(self, workspace: str | None):
        home = Path(self._home.name)
        head = {"instance": "dev", "url": f"http://127.0.0.1:{self.server.server_port}"}
        if workspace:
            head["workspace"] = workspace
        (home / ".HEAD").write_text(json.dumps(head), encoding="utf-8")

    def test_bare_call_follows_the_ambient_context(self):
        self._set_head("demo")
        code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["workspace"], "demo")
        self.assertEqual(len(_FakeOs.performed), 1)

    def test_bare_actions_follows_the_ambient_context(self):
        # Discovery routes exactly like execution: same context, same lane.
        self._set_head("demo")
        code, envelope = _run_cli("actions", "todo")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["workspace"], "demo")
        self.assertEqual(envelope["data"]["actions"][0]["ref"], "Todo.List")

    def test_bare_workspace_flag_uses_ambient(self):
        self._set_head("demo")
        code, envelope = _run_cli("actions", "todo", "--workspace")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["workspace"], "demo")
        self.assertEqual(envelope["data"]["actions"][0]["ref"], "Todo.List")

    def test_bare_call_in_machine_context_means_local(self):
        (Path(self._home.name) / ".HEAD").unlink()
        code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
        # Local lane engaged: the failure is target resolution (nothing named
        # 'todo' in the fixture store), not a missing execution flag.
        self.assertNotEqual(code, 0)
        self.assertNotIn("--local", envelope["error"]["message"])
        self.assertEqual(_FakeOs.performed, [])

    def test_bare_call_instance_only_context_refuses_not_local(self):
        # An incomplete OS target is an explicit refusal, never a silent
        # local fallback.
        self._set_head(None)
        code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
        self.assertNotEqual(code, 0)
        self.assertIn("workspace", envelope["error"]["message"])
        self.assertEqual(_FakeOs.performed, [])

    def test_corrupt_head_fails_explicitly(self):
        for content in ("{not json", "{}", json.dumps({"instance": 42, "url": "x"})):
            (Path(self._home.name) / ".HEAD").write_text(content, encoding="utf-8")
            code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
            self.assertNotEqual(code, 0, content)
            self.assertIn("corrupt", envelope["error"]["message"], content)
            self.assertEqual(_FakeOs.performed, [])

    def test_env_workspace_overrides_head(self):
        self._set_head("other")
        os.environ["TANGRAM_WORKSPACE"] = "demo"
        self.addCleanup(os.environ.pop, "TANGRAM_WORKSPACE", None)
        code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["workspace"], "demo")

    def test_env_workspace_applies_with_explicit_instance_flag(self):
        self._set_head(None)
        os.environ["TANGRAM_WORKSPACE"] = "demo"
        self.addCleanup(os.environ.pop, "TANGRAM_WORKSPACE", None)
        code, envelope = _run_cli(
            "call", "todo", "List", "--instance", "dev", "--input-json", "{}"
        )
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["workspace"], "demo")

    def test_env_instance_never_pairs_with_head_workspace(self):
        self._set_head("demo")
        os.environ["TANGRAM_INSTANCE"] = "dev"
        self.addCleanup(os.environ.pop, "TANGRAM_INSTANCE", None)
        # env instance without env workspace: the .HEAD workspace must NOT
        # ride along — the incomplete OS target refuses instead.
        code, envelope = _run_cli("call", "todo", "List", "--input-json", "{}")
        self.assertNotEqual(code, 0)
        self.assertIn("workspace", envelope["error"]["message"])
        self.assertEqual(_FakeOs.performed, [])

    def test_refusal_lists_visible_workspaces(self):
        self._set_head(None)
        code, envelope = _run_cli("actions", "todo", "--workspace")
        self.assertNotEqual(code, 0)
        message = envelope["error"]["message"]
        self.assertIn("tangram use INSTANCE/WS", message)
        self.assertIn("analytics", message)
        self.assertIn("demo", message)


if __name__ == "__main__":
    unittest.main()
