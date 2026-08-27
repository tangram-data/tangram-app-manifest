from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tangram_app import CapabilityGraph, ManifestCompilationError, compile_manifest


FIXTURE = Path(__file__).parent / "fixtures/minimal-app"


class ManifestCompilerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_compiles_validated_manifest_to_round_trippable_graph(self) -> None:
        result = compile_manifest(FIXTURE)
        graph = result.graph

        self.assertEqual(graph.package.id, "com.example/orders")
        self.assertEqual(graph.authority, "development")
        self.assertFalse(graph.development_only)
        self.assertTrue(graph.package.digest.startswith("sha256:"))
        self.assertEqual(len(graph.package.digest), 71)
        self.assertEqual(len(graph.actions), 1)

        action = graph.actions[0]
        self.assertEqual(action.id, "com.example/orders#Order.List")
        self.assertEqual(action.required_privileges, ("Order:Read",))
        self.assertFalse(action.requires_confirmation)

        binding = action.bindings[0]
        self.assertEqual(binding.id, f"{action.id}@listOrders")
        self.assertEqual(binding.method, "GET")
        self.assertEqual(binding.path, "/orders")
        self.assertEqual(binding.input_schema["properties"]["status"]["type"], "string")
        self.assertEqual(binding.input_bindings["status"].location, "query")
        self.assertEqual(binding.input_bindings["status"].name, "status")

        round_tripped = CapabilityGraph.from_json(graph.to_json())
        self.assertEqual(round_tripped.to_dict(), graph.to_dict())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dist/tangram-app.json"
            graph.write_file(output)
            self.assertEqual(
                CapabilityGraph.from_file(output).to_dict(),
                graph.to_dict(),
            )

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_package_digest_is_deterministic_and_content_sensitive(self) -> None:
        first = compile_manifest(FIXTURE).graph.package.digest
        second = compile_manifest(FIXTURE).graph.package.digest
        self.assertEqual(first, second)

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            app_file = copied / "manifests/app.pkl"
            app_file.write_text(
                app_file.read_text(encoding="utf-8") + "\n// digest change\n",
                encoding="utf-8",
            )
            changed = compile_manifest(copied).graph.package.digest
        self.assertNotEqual(changed, first)

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_flat_input_projection_records_collision_reverse_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            openapi = copied / "manifests/api/open_api.yml"
            document = json.loads(openapi.read_text(encoding="utf-8"))
            operation = document["paths"].pop("/orders")["get"]
            operation["parameters"] = [
                {
                    "name": "status",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "status",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                },
            ]
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"status": {"type": "integer"}},
                            "required": ["status"],
                        }
                    }
                },
            }
            document["paths"]["/orders/{status}"] = {"get": operation}
            openapi.write_text(json.dumps(document), encoding="utf-8")

            binding = compile_manifest(copied).graph.actions[0].bindings[0]

        self.assertTrue(binding.body_required)
        self.assertEqual(
            set(binding.input_schema["properties"]),
            {"path_status", "query_status", "body_status"},
        )
        self.assertEqual(binding.input_bindings["path_status"].location, "path")
        self.assertEqual(binding.input_bindings["query_status"].location, "query")
        self.assertEqual(binding.input_bindings["body_status"].location, "body")
        self.assertEqual(
            set(binding.input_schema["required"]),
            {"path_status", "body_status"},
        )

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_unsupported_cookie_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            openapi = copied / "manifests/api/open_api.yml"
            document = json.loads(openapi.read_text(encoding="utf-8"))
            operation = document["paths"]["/orders"]["get"]
            operation["parameters"].append(
                {
                    "name": "session",
                    "in": "cookie",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )
            openapi.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                ManifestCompilationError, "unsupported cookie parameter"
            ):
                compile_manifest(copied)

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_documented_2xx_response_is_preferred_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            openapi = copied / "manifests/api/open_api.yml"
            document = json.loads(openapi.read_text(encoding="utf-8"))
            responses = document["paths"]["/orders"]["get"]["responses"]
            responses.clear()
            responses["default"] = {
                "description": "Error",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"error": {"type": "string"}},
                        }
                    }
                },
            }
            responses["299"] = {
                "description": "Success",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"orders": {"type": "array"}},
                        }
                    }
                },
            }
            openapi.write_text(json.dumps(document), encoding="utf-8")

            output = compile_manifest(copied).graph.actions[0].bindings[0].output_schema

        self.assertIsNotNone(output)
        assert output is not None
        self.assertIn("orders", output["properties"])
        self.assertNotIn("error", output["properties"])

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_managed_authentication_headers_are_not_agent_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            openapi = copied / "manifests/api/open_api.yml"
            document = json.loads(openapi.read_text(encoding="utf-8"))
            document["components"] = {
                "securitySchemes": {
                    "apiKey": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-API-Key",
                    }
                }
            }
            operation = document["paths"]["/orders"]["get"]
            operation["parameters"].extend(
                [
                    {
                        "name": "Authorization",
                        "in": "header",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "X-API-Key",
                        "in": "header",
                        "schema": {"type": "string"},
                    },
                ]
            )
            openapi.write_text(json.dumps(document), encoding="utf-8")

            binding = compile_manifest(copied).graph.actions[0].bindings[0]

        self.assertEqual(set(binding.input_schema["properties"]), {"status"})
        self.assertEqual(set(binding.input_bindings), {"status"})

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_custom_action_without_privilege_uses_scala_write_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "package"
            shutil.copytree(FIXTURE, copied)
            resources = copied / "manifests/api/resources.pkl"
            text = resources.read_text(encoding="utf-8")
            text = text.replace('name = "List"', 'name = "Archive"', 1)
            text = text.replace('            privilege = "Read"\n', "", 1)
            resources.write_text(text, encoding="utf-8")

            action = compile_manifest(copied).graph.actions[0]

        self.assertEqual(action.required_privileges, ("Order:Write",))


if __name__ == "__main__":
    unittest.main()
