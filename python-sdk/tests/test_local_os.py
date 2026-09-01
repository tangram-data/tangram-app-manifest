"""OS-install lane: credential reuse, package-tree contract, wire shape."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tangram_app.local_os import (
    LocalOsError,
    base_url,
    load_os_credential,
    os_install,
    read_tree,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-app"


class _FakeOs(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _FakeOs.seen = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length)),
        }
        payload = json.dumps({"plan": [{"name": "backend", "action": "create"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class LocalOsTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)

    def _write_credentials(self, url: str):
        home = Path(self._home.name)
        home.mkdir(exist_ok=True)
        (home / ".credentials").write_text(
            json.dumps(
                [
                    {"instance": "local", "url": "localhost", "token": "tok-local"},
                    {"instance": "dev", "url": url, "token": "tok-dev"},
                ]
            )
        )
        (home / ".HEAD").write_text(json.dumps({"instance": "dev", "url": url}))

    def test_credential_resolution_and_base_url(self):
        self._write_credentials("os.example.com")
        head = load_os_credential()
        self.assertEqual(head["instance"], "dev")
        self.assertEqual(base_url(head), "https://os.example.com:443")
        local = load_os_credential("local")
        self.assertEqual(base_url(local), "http://localhost:8081")
        with self.assertRaises(LocalOsError):
            load_os_credential("ghost")

    def test_read_tree_contract(self):
        tree = read_tree(FIXTURE)
        self.assertIn("manifests/app.pkl", tree)
        self.assertNotIn("README.md", tree)  # top-level working-copy noise
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifests").mkdir()
            (root / "manifests" / "app.pkl").write_text("x = 1")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "secret").write_text("no")
            (root / "manifests" / "blob.bin").write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(LocalOsError) as caught:
                read_tree(root)
            self.assertIn("UTF-8", str(caught.exception))
            (root / "manifests" / "blob.bin").unlink()
            tree = read_tree(root)
            self.assertEqual(list(tree), ["manifests/app.pkl"])  # dot-dirs skipped

    def test_os_install_posts_the_native_wire_shape(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self._write_credentials(f"http://127.0.0.1:{server.server_port}")

        outcome = os_install(FIXTURE, "demo ws", dry_run=True, upgrade=True)
        self.assertEqual(outcome["workspace"], "demo ws")
        self.assertEqual(outcome["result"]["plan"][0]["action"], "create")
        self.assertEqual(_FakeOs.seen["auth"], "Bearer tok-dev")
        self.assertEqual(
            _FakeOs.seen["path"], "/api/core/v1/workspaces/demo%20ws/apps:install"
        )
        body = _FakeOs.seen["body"]
        self.assertEqual(body["source"]["kind"], "local-package")
        self.assertTrue(body["dryRun"])
        self.assertTrue(body["upgrade"])
        self.assertIn("manifests/app.pkl", body["source"]["files"])

    def test_explicit_token_requires_url_and_vice_versa(self):
        with self.assertRaises(LocalOsError):
            os_install(FIXTURE, "demo", token="t")
        with self.assertRaises(LocalOsError):
            os_install(FIXTURE, "demo", url="https://x.example.com")


if __name__ == "__main__":
    unittest.main()
