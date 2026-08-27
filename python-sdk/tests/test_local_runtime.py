from __future__ import annotations

from pathlib import Path
import json
import tempfile
import textwrap
import unittest
from urllib.request import Request, urlopen

from tangram_app import (
    BackendContractError,
    BackendSpec,
    CapabilityGraph,
    LocalRuntimeError,
    TangramApp,
    TangramProject,
    UnsupportedRequirementError,
)
from tangram_app.local_runtime import _local_provider_plan


def graph_dict(*, operation_id: str = "listOrders", path: str = "/orders") -> dict:
    action_id = "com.example/orders#Order.List"
    return {
        "formatVersion": "1",
        "authority": "development",
        "manifestSpecVersion": "v1",
        "package": {
            "id": "com.example/orders",
            "version": "0.1.0",
            "digest": "sha256:local-runtime-fixture",
        },
        "actions": [
            {
                "id": action_id,
                "resourceType": "Order",
                "name": "List",
                "description": "List orders",
                "effect": "Stateless",
                "idempotent": True,
                "requiresConfirmation": False,
                "requiredPrivileges": ["Order:Read"],
                "bindings": [
                    {
                        "id": f"{action_id}@{operation_id}",
                        "operationId": operation_id,
                        "method": "GET",
                        "path": path,
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                        },
                        "outputSchema": {"type": "object"},
                    }
                ],
            }
        ],
        "runtimeRequirements": {
            "backend": "service",
            "settings": [],
            "secrets": [],
            "infrastructureClaims": [],
        },
    }


_FAKE_UVICORN = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

port = int(sys.argv[sys.argv.index("--port") + 1])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/openapi.json":
            value = {
                "openapi": "3.0.0",
                "paths": {
                    "/orders": {
                        "get": {"operationId": "listOrders", "responses": {"200": {}}}
                    }
                },
            }
        elif self.path == "/orders":
            value = {"orders": [], "status": None}
        else:
            self.send_error(404)
            return
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def write_backend(root: Path, *, entry: str = "main") -> None:
    source = root / "manifests/deployment/source/backend/src"
    source.mkdir(parents=True)
    (root / "manifests").mkdir(exist_ok=True)
    (source.parent / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [project]
            name = "fixture"
            version = "0.0.0"
            requires-python = ">=3.12"
            dependencies = []

            [tool.tangram.backend]
            runtime = "python-3.12"
            entry = "{entry}"
            egress = []
            """
        ),
        encoding="utf-8",
    )
    entry_file = source.joinpath(*entry.split(".")).with_suffix(".py")
    entry_file.parent.mkdir(parents=True, exist_ok=True)
    entry_file.write_text("app = object()\n", encoding="utf-8")
    (source / "uvicorn.py").write_text(_FAKE_UVICORN, encoding="utf-8")


def write_ui(root: Path) -> tuple[Path, Path]:
    component = root / "manifests/ui/components/orders"
    component.mkdir(parents=True)
    (component / "index.tsx").write_text(
        "export default function App(){ return null }\n", encoding="utf-8"
    )
    modules = root / "test-node-modules"
    modules.mkdir()
    esbuild = root / "fake-esbuild"
    esbuild.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import pathlib
            import sys
            output = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--outfile="))
            pathlib.Path(output).write_text("window.__ui_loaded__=true")
            """
        ),
        encoding="utf-8",
    )
    esbuild.chmod(0o700)
    return esbuild, modules


class BackendSpecTests(unittest.TestCase):
    def test_reads_canonical_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root, entry="orders.api")
            spec = BackendSpec.from_project(root)

        self.assertEqual(spec.entry, "orders.api")
        self.assertEqual(spec.runtime, "python-3.12")
        self.assertEqual(spec.dependencies, ())

    def test_rejects_entry_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            pyproject = root / "manifests/deployment/source/backend/pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    'entry = "main"', 'entry = "../main"'
                ),
                encoding="utf-8",
            )
            with self.assertRaises(LocalRuntimeError):
                BackendSpec.from_project(root)


class LocalSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_backend_and_uses_governed_call_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            app = TangramApp(
                graph=CapabilityGraph.from_dict(graph_dict()),
                authority="development",
                source_root=root,
            )

            with app.run_local(
                startup_timeout_seconds=5, managed_environment=False
            ) as running:
                self.assertTrue(running.process.poll() is None)
                self.assertEqual(
                    running.capabilities()["runtime"]["kind"], "local-source"
                )
                result = await running.call("com.example/orders#Order.List", {})
                self.assertEqual(result, {"orders": [], "status": None})
                process = running.process

            self.assertIsNotNone(process.poll())

    async def test_refuses_backend_missing_declared_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            app = TangramApp(
                graph=CapabilityGraph.from_dict(
                    graph_dict(operation_id="missingOperation")
                ),
                source_root=root,
            )

            with self.assertRaises(BackendContractError):
                app.run_local(startup_timeout_seconds=5, managed_environment=False)

    async def test_refuses_served_method_or_path_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            app = TangramApp(
                graph=CapabilityGraph.from_dict(graph_dict(path="/other-orders")),
                source_root=root,
            )

            with self.assertRaises(BackendContractError):
                app.run_local(startup_timeout_seconds=5, managed_environment=False)

    async def test_refuses_unconfigured_local_platform_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            value = graph_dict()
            value["runtimeRequirements"]["infrastructureClaims"] = [
                {"name": "deployment/dependencies.pkl", "required": True}
            ]
            app = TangramApp(
                graph=CapabilityGraph.from_dict(value),
                source_root=root,
            )

            with self.assertRaises(UnsupportedRequirementError):
                app.run_local(startup_timeout_seconds=5)

    def test_recognizes_canonical_postgres_claim_as_local_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = root / "manifests/deployment/dependencies.pkl"
            dependencies.parent.mkdir(parents=True)
            dependencies.write_text(
                'main: infra.PostgresqlDatabaseClaim = new {}\n', encoding="utf-8"
            )
            value = graph_dict()
            value["runtimeRequirements"]["infrastructureClaims"] = [
                {"name": "deployment/dependencies.pkl", "required": True}
            ]
            app = TangramApp(
                graph=CapabilityGraph.from_dict(value),
                source_root=root,
            )

            unsupported, needs_postgres = _local_provider_plan(app)

            self.assertEqual(unsupported, ())
            self.assertTrue(needs_postgres)

    def test_project_facade_opens_source_and_exposes_backend_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            project = TangramProject.open(root)
            self.assertEqual(project.backend_spec().entry, "main")

    def test_serves_declared_ui_and_routes_browser_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_backend(root)
            esbuild, modules = write_ui(root)
            value = graph_dict()
            value["ui"] = {
                "mode": "UIComponent",
                "rootComponent": "orders",
                "name": "orders",
                "kind": "sandboxed",
                "entry": "components/orders/index.tsx",
                "surfaces": ["app-page"],
            }
            app = TangramApp(
                graph=CapabilityGraph.from_dict(value),
                authority="development",
                source_root=root,
            )

            with app.run_local(
                startup_timeout_seconds=5,
                managed_environment=False,
                environment={
                    "TANGRAM_ESBUILD_BIN": str(esbuild),
                    "TANGRAM_COMPONENT_NODE_MODULES": str(modules),
                },
            ) as running:
                self.assertIsNotNone(running.ui_url)
                with urlopen(running.ui_url, timeout=5) as response:
                    self.assertIn(b"/bundle.js", response.read())
                request = Request(
                    running.ui_url + "action",
                    data=json.dumps(
                        {
                            "resourceType": "Order",
                            "action": "List",
                            "args": {},
                        }
                    ).encode(),
                    method="POST",
                    headers={"content-type": "application/json"},
                )
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(
                    result["envelope"]["data"], {"orders": [], "status": None}
                )


if __name__ == "__main__":
    unittest.main()
