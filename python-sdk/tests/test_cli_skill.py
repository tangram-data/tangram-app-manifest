from __future__ import annotations

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

try:
    import yaml
except ModuleNotFoundError:  # Exercised in installed-package tests.
    yaml = None

from tangram_app import (
    CapabilityGraph,
    CapabilityGraphStaleError,
    TangramApp,
    generate_skill,
    verify_skill,
)
from tangram_app.cli import main
from test_local_runtime import write_backend


FIXTURE = Path(__file__).parent / "fixtures/minimal-app"
SDK_SRC = Path(__file__).parents[1] / "src"


class _OrdersHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/error":
            self.send_error(503)
            return
        payload = b'{"orders":[],"status":null}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args) -> None:
        return None


def run_cli(arguments: list[str]) -> tuple[int, dict]:
    output = StringIO()
    with redirect_stdout(output):
        status = main(arguments)
    lines = output.getvalue().splitlines()
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {lines!r}")
    return status, json.loads(lines[0])


class CliAndSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _OrdersHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.backend = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_facade_discovers_tools_and_reports_degraded_capabilities(self) -> None:
        app = TangramApp.from_package(FIXTURE)

        self.assertEqual(len(app.tools()), 1)
        self.assertEqual(app.tools()[0].id, "com.example/orders#Order.List@listOrders")
        report = app.capabilities()
        self.assertEqual(report["authority"], "development")
        self.assertEqual(report["capabilities"]["authorization"]["state"], "emulated")
        self.assertEqual(report["capabilities"]["oauth"]["state"], "unsupported")

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_cli_inspect_and_build_use_one_versioned_json_envelope(self) -> None:
        status, inspected = run_cli(["inspect", str(FIXTURE), "--tools"])
        self.assertEqual(status, 0)
        self.assertTrue(inspected["ok"])
        self.assertEqual(inspected["schemaVersion"], "1")
        self.assertEqual(len(inspected["data"]["tools"]), 1)

        with tempfile.TemporaryDirectory() as directory:
            graph_path = Path(directory) / "graph.json"
            status, built = run_cli(
                ["build", str(FIXTURE), "--output", str(graph_path)]
            )
            self.assertEqual(status, 0)
            self.assertEqual(built["data"]["authority"], "development")
            self.assertTrue(graph_path.is_file())

            status, snapshot = run_cli(["inspect", str(graph_path), "--tools"])
            self.assertEqual(status, 0)
            self.assertEqual(
                snapshot["data"]["package"]["digest"],
                built["data"]["packageDigest"],
            )

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_cli_validates_and_calls_canonical_local_source_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package"
            shutil.copytree(FIXTURE, package)
            write_backend(package)

            status, validated = run_cli(["validate", str(package)])
            self.assertEqual(status, 0)
            self.assertTrue(validated["data"]["valid"])

            status, called = run_cli(
                [
                    "call",
                    str(package),
                    "com.example/orders#Order.List@listOrders",
                    "--local",
                    "--input-json",
                    "{}",
                    "--startup-timeout",
                    "5",
                ]
            )
            self.assertEqual(status, 0)
            self.assertEqual(called["data"]["result"]["orders"], [])

            running = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tangram_app",
                    "run",
                    str(package),
                    "--startup-timeout",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": str(SDK_SRC)},
            )
            assert running.stdout is not None
            started = json.loads(running.stdout.readline())
            self.assertEqual(started["data"]["runtime"], "local-source")
            self.assertIsNone(started["data"]["uiUrl"])
            running.terminate()
            _, run_stderr = running.communicate(timeout=10)
            self.assertEqual(running.returncode, 0, run_stderr)

            skill = Path(directory) / "orders-skill"
            status, generated = run_cli(
                ["skill", "generate", str(package), "--output", str(skill)]
            )
            self.assertEqual(status, 0)
            self.assertEqual(generated["data"]["bindings"], 1)
            process = subprocess.run(
                [
                    sys.executable,
                    str(skill / "scripts/tangram_agent.py"),
                    "call",
                    "com.example/orders#Order.List@listOrders",
                    "--local-package",
                    str(package),
                    "--startup-timeout",
                    "5",
                ],
                input="{}",
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(SDK_SRC)},
            )
            self.assertEqual(process.returncode, 0, process.stderr or process.stdout)
            self.assertEqual(json.loads(process.stdout)["data"]["result"]["orders"], [])

    def test_cli_argument_errors_use_one_versioned_json_envelope(self) -> None:
        status, envelope = run_cli(["inspect"])
        self.assertEqual(status, 2)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_generated_skill_is_valid_locked_and_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "orders"
            status, generated = run_cli(
                ["skill", "generate", str(FIXTURE), "--output", str(skill)]
            )
            self.assertEqual(status, 0)
            self.assertEqual(generated["data"]["bindings"], 1)
            self.assertEqual(verify_skill(skill).package.id, "com.example/orders")
            self.assertFalse((skill / "README.md").exists())
            generated_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in skill.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(str(FIXTURE.resolve()), generated_text)

            process = subprocess.run(
                [sys.executable, str(skill / "scripts/tangram_agent.py"), "inspect"],
                check=False,
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(SDK_SRC)},
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            envelope = json.loads(process.stdout)
            self.assertTrue(envelope["ok"])
            self.assertEqual(len(envelope["data"]["tools"]), 1)

            called = subprocess.run(
                [
                    sys.executable,
                    str(skill / "scripts/tangram_agent.py"),
                    "call",
                    "com.example/orders#Order.List@listOrders",
                    "--backend",
                    self.backend,
                ],
                input="{}",
                check=False,
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(SDK_SRC)},
            )
            self.assertEqual(called.returncode, 0, called.stderr)
            call_envelope = json.loads(called.stdout)
            self.assertTrue(call_envelope["ok"])
            self.assertEqual(call_envelope["data"]["result"]["orders"], [])

            graph = skill / "references/capability-graph.json"
            graph.write_text(graph.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(CapabilityGraphStaleError):
                verify_skill(skill)


def graph_dict(*, backend: str = "service", path: str = "/orders") -> dict:
    return {
        "formatVersion": "1",
        "authority": "development",
        "developmentOnly": True,
        "manifestSpecVersion": "v1",
        "package": {
            "id": "com.example/orders",
            "version": "0.1.0",
            "digest": "sha256:fixture",
        },
        "actions": [
            {
                "id": "com.example/orders#Order.List",
                "resourceType": "Order",
                "name": "List",
                "description": "List orders",
                "effect": "Stateless",
                "idempotent": True,
                "requiresConfirmation": False,
                "requiredPrivileges": ["Order:Read"],
                "bindings": [
                    {
                        "id": "com.example/orders#Order.List@listOrders",
                        "operationId": "listOrders",
                        "method": "GET",
                        "path": path,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"filter": {"type": "object"}},
                            "additionalProperties": False,
                        },
                        "inputBindings": {
                            "filter": {"location": "query", "name": "filter"}
                        },
                        "outputSchema": {"type": "object"},
                    }
                ],
            }
        ],
        "runtimeRequirements": {
            "backend": backend,
            "settings": [],
            "secrets": [],
            "infrastructureClaims": [],
        },
    }


class GraphCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _OrdersHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.backend = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def _write_graph(self, directory: str, value: dict) -> Path:
        path = Path(directory) / "graph.json"
        CapabilityGraph.from_dict(value).write_file(path)
        return path

    def test_snapshot_preserves_provenance_and_delegates_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_graph(directory, graph_dict())
            status, envelope = run_cli(["inspect", str(path), "--tools"])
        self.assertEqual(status, 0)
        report = envelope["data"]["capabilityReport"]
        self.assertEqual(report["authority"], "development")
        self.assertTrue(report["developmentOnly"])
        self.assertEqual(
            report["capabilities"]["manifestValidation"],
            {"state": "delegated", "detail": "compiled-artifact"},
        )

    def test_help_is_returned_as_one_json_envelope(self) -> None:
        for arguments in (["--help"], ["call", "--help"]):
            status, envelope = run_cli(list(arguments))
            self.assertEqual(status, 0)
            self.assertTrue(envelope["ok"])
            self.assertIn("usage:", envelope["data"]["help"])

    def test_error_classes_distinguish_input_runtime_and_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_path = self._write_graph(directory, graph_dict())
            status, envelope = run_cli(
                ["inspect", str(graph_path), "--action", "unknown-action"]
            )
            self.assertEqual(status, 3)
            self.assertEqual(envelope["error"]["code"], "unknown_binding")

            status, envelope = run_cli(
                [
                    "call",
                    str(graph_path),
                    "com.example/orders#Order.List@listOrders",
                    "--backend",
                    self.backend,
                    "--input-json",
                    '{"filter":{"status":"open"}}',
                ]
            )
            self.assertEqual(status, 3)
            self.assertEqual(envelope["error"]["code"], "invalid_input")

            connector = graph_dict(backend="connector")
            connector["runtimeRequirements"]["secrets"] = [
                {"name": "API_TOKEN", "required": True}
            ]
            connector_path = self._write_graph(directory, connector)
            status, envelope = run_cli(
                [
                    "call",
                    str(connector_path),
                    "com.example/orders#Order.List@listOrders",
                    "--backend",
                    self.backend,
                    "--input-json",
                    "{}",
                ]
            )
            self.assertEqual(envelope["error"]["code"], "unsupported_requirement")

            upstream_path = self._write_graph(directory, graph_dict(path="/error"))
            status, envelope = run_cli(
                [
                    "call",
                    str(upstream_path),
                    "com.example/orders#Order.List@listOrders",
                    "--backend",
                    self.backend,
                    "--input-json",
                    "{}",
                ]
            )
            self.assertEqual(envelope["error"]["code"], "upstream_failed")

    def test_generated_yaml_is_parseable_without_pkl(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML is not installed")
        app = TangramApp.from_graph(CapabilityGraph.from_dict(graph_dict()))
        with tempfile.TemporaryDirectory() as directory:
            skill = generate_skill(app, Path(directory) / "orders")
            markdown = (skill / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = markdown.split("---", 2)[1]
            self.assertEqual(yaml.safe_load(frontmatter)["name"], "orders")
            metadata = yaml.safe_load(
                (skill / "agents/openai.yaml").read_text(encoding="utf-8")
            )
            self.assertIn("default_prompt", metadata["interface"])


if __name__ == "__main__":
    unittest.main()
