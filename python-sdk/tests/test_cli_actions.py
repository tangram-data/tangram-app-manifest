"""Action ergonomics: short refs, the actions catalog, warm-session reuse."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from tangram_app.cli_actions import (
    ActionRefError,
    attach_url,
    record_session,
    resolve_action_ref,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-app"


def _app(*actions):
    return SimpleNamespace(graph=SimpleNamespace(actions=list(actions)))


def _action(resource_type, name):
    return SimpleNamespace(
        id=f"com.example/demo#{resource_type}.{name}",
        resource_type=resource_type,
        name=name,
    )


class ResolveActionRefTest(unittest.TestCase):
    def test_short_forms_and_passthrough(self):
        app = _app(_action("Todo", "List"), _action("Todo", "Create"))
        self.assertEqual(resolve_action_ref(app, "List"), "com.example/demo#Todo.List")
        self.assertEqual(resolve_action_ref(app, "Todo.Create"), "com.example/demo#Todo.Create")
        self.assertEqual(
            resolve_action_ref(app, "com.example/demo#Todo.List@op"),
            "com.example/demo#Todo.List@op",
        )

    def test_ambiguous_and_unknown(self):
        app = _app(_action("Todo", "List"), _action("Note", "List"))
        with self.assertRaises(ActionRefError) as caught:
            resolve_action_ref(app, "List")
        self.assertIn("Todo.List", str(caught.exception))
        self.assertEqual(resolve_action_ref(app, "Note.List"), "com.example/demo#Note.List")
        with self.assertRaises(ActionRefError) as caught:
            resolve_action_ref(app, "Ghost")
        self.assertIn("declared:", str(caught.exception))


class _Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/openapi.json":
            payload = b"{}"
        elif self.path == "/orders":
            payload = json.dumps([{"id": 7}]).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class WarmSessionTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_attach_url_liveness_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = root / ".preview"
            preview.mkdir()
            self.assertIsNone(attach_url(root))  # no marker
            record_session(preview, self.url, os.getpid())
            self.assertEqual(attach_url(root), self.url)
            record_session(preview, self.url, 2**22 + 12345)  # dead pid
            self.assertIsNone(attach_url(root))
            record_session(preview, "http://10.0.0.9:1", os.getpid())  # non-loopback
            self.assertIsNone(attach_url(root))
            record_session(preview, f"http://127.0.0.1:1", os.getpid())  # not serving
            self.assertIsNone(attach_url(root))

    def test_call_local_attaches_with_short_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            shutil.copytree(FIXTURE, root)
            (root / ".preview").mkdir()
            record_session(root / ".preview", self.url, os.getpid())
            completed = subprocess.run(
                [
                    sys.executable, "-m", "tangram_app", "call", str(root),
                    "Order.List", "--local", "--input-json", "{}",
                ],
                capture_output=True,
                text=True,
                env={
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    "PATH": os.environ["PATH"],
                    "HOME": os.environ.get("HOME", ""),
                },
            )
            envelope = json.loads(completed.stdout)
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["data"]["result"], [{"id": 7}])
            self.assertEqual(envelope["data"]["session"], "attached")
            self.assertTrue(envelope["data"]["bindingId"].endswith("@listOrders"))

    def test_actions_catalog_lists_refs(self):
        completed = subprocess.run(
            [sys.executable, "-m", "tangram_app", "actions", str(FIXTURE)],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": os.environ["PATH"],
                "HOME": os.environ.get("HOME", ""),
            },
        )
        envelope = json.loads(completed.stdout)
        self.assertTrue(envelope["ok"], envelope)
        rows = envelope["data"]["actions"]
        self.assertEqual(rows[0]["ref"], "Order.List")
        self.assertEqual(rows[0]["effect"], "Stateless")
        self.assertEqual(rows[0]["bindings"], ["listOrders"])


if __name__ == "__main__":
    unittest.main()
