"""Install real Pkl packages and call their HTTP bindings through the hub host."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from tangram_app import cli
from tangram_app.cli_actions import record_session
from tangram_app.local_store import install_app


class HubAcceptanceTest(unittest.TestCase):
    def test_new_apps_are_discovered_inspected_and_invoked_without_skill_sync(self):
        class Backend(BaseHTTPRequestHandler):
            calls = []
            def do_GET(self):
                self.calls.append(self.path)
                body = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass

        def run(*args):
            out = StringIO()
            with redirect_stdout(out):
                status = cli.main(["hub", *args])
            result = json.loads(out.getvalue())
            self.assertEqual(status, 0, result)
            return result["data"]

        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "TANGRAM_HOME": folder, "TANGRAM_INSTANCE": "local", "TANGRAM_WORKSPACE": "",
                "TANGRAM_AGENT_HOME": folder}):
                fixture = Path(__file__).parent / "fixtures" / "minimal-app"
                pin = run("resolve", "--local")["targetRef"]
                self.assertEqual(run("search", "reimbursements", "--target-ref", pin)["candidates"], [])
                for name, description in [("orders", "customer orders"), ("expenses", "pending reimbursements")]:
                    source = Path(folder) / "source" / name
                    shutil.copytree(fixture, source)
                    manifest = source / "manifests" / "app.pkl"
                    manifest.write_text(manifest.read_text().replace('name = "orders"', f'name = "{name}"'))
                    resource = source / "manifests" / "api" / "resources.pkl"
                    resource.write_text(resource.read_text().replace("List orders", f"List {description}"))
                    api = source / "manifests" / "api" / "open_api.yml"
                    api.write_text(api.read_text().replace('"/orders"', f'"/{name}"'))
                    installed = install_app(str(source))
                    preview = Path(installed["root"]) / ".preview"
                    preview.mkdir()
                    record_session(preview, f"http://127.0.0.1:{server.server_port}", os.getpid())
                    candidate = run("search", description, "--target-ref", pin)["candidates"][0]
                    self.assertTrue(candidate["action"].startswith(f"com.example/{name}#"))
                    arguments = [candidate["action"], "--contract", candidate["contract"], "--target-ref", pin]
                    inspected = run("inspect", *arguments)
                    self.assertIn("status", inspected["inputSchema"]["properties"])
                    call = ["call", *arguments, "--invocation-id", str(uuid.uuid4()), "--input-json", '{"status":"pending"}']
                    completed = run(*call)
                    self.assertEqual(completed["state"], "succeeded")
                    self.assertEqual(completed["result"]["result"]["path"], f"/{name}?status=pending")
                    self.assertEqual(run(*call), completed)
                self.assertEqual([path for path in Backend.calls if "status=pending" in path],
                                 ["/orders?status=pending", "/expenses?status=pending"])
                self.assertFalse((Path(folder) / ".codex").exists())
                self.assertFalse((Path(folder) / ".claude").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
