"""Developer connections: storage, spec rendering, and connected calls."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tangram_app.local_connections import (
    LocalConnectionError,
    delete_connection,
    load_connection,
    load_connector_spec,
    render_connection,
    save_connection,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal-connector"

SPEC = {
    "endpoint": "https://api.example.com",
    "endpointOverridable": True,
    "endpointHostAllowlist": ["api.example.com", "*.svc.example.com"],
    "auth": {"httpHeaders": {"Authorization": {"template": "Bearer {{oauth.accessToken}}"}}},
}


class ConnectionStoreTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TANGRAM_HOME"] = self._home.name
        self.addCleanup(os.environ.pop, "TANGRAM_HOME", None)

    def test_save_load_delete_and_permissions(self):
        stored = save_connection("com.example/echo", "tok-123", tenant="realm-9")
        mode = stat.S_IMODE(stored.stat().st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)
        entry = load_connection("com.example/echo")
        self.assertEqual(entry["token"], "tok-123")
        self.assertEqual(entry["tenant"], "realm-9")
        self.assertTrue(delete_connection("com.example/echo"))
        self.assertIsNone(load_connection("com.example/echo"))
        self.assertFalse(delete_connection("com.example/echo"))
        with self.assertRaises(LocalConnectionError):
            save_connection("com.example/echo", "   ")


class RenderConnectionTest(unittest.TestCase):
    def test_renders_endpoint_and_headers(self):
        endpoint, headers = render_connection(SPEC, {"token": "tok"})
        self.assertEqual(endpoint, "https://api.example.com")
        self.assertEqual(headers, {"Authorization": "Bearer tok"})

    def test_allowlist_wildcard_and_mismatch(self):
        good = dict(SPEC, endpoint="https://a.svc.example.com")
        endpoint, _ = render_connection(good, {"token": "t"})
        self.assertEqual(endpoint, "https://a.svc.example.com")
        with self.assertRaises(LocalConnectionError):
            render_connection(dict(SPEC, endpoint="https://evil.example.net"), {"token": "t"})

    def test_https_required_except_loopback_override(self):
        with self.assertRaises(LocalConnectionError):
            render_connection(dict(SPEC, endpoint="http://api.example.com"), {"token": "t"})
        endpoint, _ = render_connection(
            SPEC, {"token": "t"}, endpoint_override="http://127.0.0.1:9"
        )
        self.assertEqual(endpoint, "http://127.0.0.1:9")
        pinned = dict(SPEC, endpointOverridable=False)
        with self.assertRaises(LocalConnectionError):
            render_connection(pinned, {"token": "t"}, endpoint_override="https://x.example.com")

    def test_override_still_enforces_allowlist_for_remote_hosts(self):
        with self.assertRaises(LocalConnectionError):
            render_connection(
                SPEC, {"token": "t"}, endpoint_override="https://attacker.example.net"
            )
        endpoint, _ = render_connection(
            SPEC, {"token": "t"}, endpoint_override="https://b.svc.example.com"
        )
        self.assertEqual(endpoint, "https://b.svc.example.com")

    def test_unknown_placeholder_and_tenant(self):
        spec = dict(
            SPEC,
            auth={"httpHeaders": {"X-Tenant": {"template": "{{oauth.tenantId}}"}}},
        )
        with self.assertRaises(LocalConnectionError):
            render_connection(spec, {"token": "t"})
        _, headers = render_connection(spec, {"token": "t", "tenant": "realm-1"})
        self.assertEqual(headers["X-Tenant"], "realm-1")


class _Vendor(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_GET(self):
        _Vendor.seen = {"path": self.path, "auth": self.headers.get("Authorization")}
        payload = json.dumps([{"echo": 1}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class ConnectedCallTest(unittest.TestCase):
    """connect + call --connected against a loopback fake vendor."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Vendor)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

    def _cli(self, *arguments):
        import subprocess

        completed = subprocess.run(
            [sys.executable, "-m", "tangram_app", *arguments],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": os.environ["PATH"],
                "TANGRAM_HOME": self._home.name,
                "HOME": os.environ.get("HOME", ""),
            },
        )
        return json.loads(completed.stdout)

    def test_connect_then_connected_call_hits_the_vendor(self):
        connected = self._cli("connect", str(FIXTURE), "--token", "tok-e2e")
        self.assertTrue(connected["ok"], connected)
        vendor = f"http://127.0.0.1:{self.server.server_port}"
        envelope = self._cli(
            "call", str(FIXTURE), "com.example/echo#Echo.List@listEcho",
            "--connected", "--endpoint", vendor, "--input-json", "{}",
        )
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["data"]["result"], [{"echo": 1}])
        self.assertEqual(_Vendor.seen["auth"], "Bearer tok-e2e")
        self.assertEqual(_Vendor.seen["path"], "/echo")

    def test_connected_call_without_connection_is_actionable(self):
        envelope = self._cli(
            "call", str(FIXTURE), "com.example/echo#Echo.List@listEcho",
            "--connected", "--input-json", "{}",
        )
        self.assertFalse(envelope["ok"])
        self.assertIn("tangram-app connect", envelope["error"]["message"])

    def test_spec_loading_refuses_non_connectors(self):
        with self.assertRaises(LocalConnectionError):
            load_connector_spec(Path(__file__).resolve().parent / "fixtures" / "minimal-app")


if __name__ == "__main__":
    unittest.main()
