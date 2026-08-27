from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tangram_app import (
    AmbiguousActionError,
    CapabilityGraph,
    CapabilityGraphError,
    ConfirmationRequiredError,
    InMemoryDriver,
    InputValidationError,
    LocalDevelopmentPolicy,
    MemoryAuditSink,
    OutputValidationError,
    PolicyDeniedError,
    TangramApp,
    TangramHost,
)


def graph_dict() -> dict:
    return {
        "formatVersion": "1",
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
                        "path": "/orders",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                            "additionalProperties": False,
                        },
                        "outputSchema": {"type": "object"},
                    }
                ],
            },
            {
                "id": "com.example/orders#Order.Approve",
                "resourceType": "Order",
                "name": "Approve",
                "description": "Approve an order",
                "effect": "Reversible",
                "idempotent": False,
                "requiresConfirmation": True,
                "requiredPrivileges": ["Order:Write"],
                "bindings": [
                    {
                        "id": "com.example/orders#Order.Approve@approveOrder",
                        "operationId": "approveOrder",
                        "method": "POST",
                        "path": "/orders/{orderId}/approve",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "orderId": {"type": "integer", "minimum": 1}
                            },
                            "required": ["orderId"],
                            "additionalProperties": False,
                        },
                    }
                ],
            },
            {
                "id": "com.example/orders#Order.Export",
                "resourceType": "Order",
                "name": "Export",
                "description": "Export orders",
                "effect": "Stateless",
                "idempotent": True,
                "requiresConfirmation": False,
                "requiredPrivileges": ["Order:Read"],
                "bindings": [
                    {
                        "id": "com.example/orders#Order.Export@exportCsv",
                        "operationId": "exportCsv",
                        "method": "GET",
                        "path": "/orders.csv",
                        "inputSchema": {"type": "object"},
                    },
                    {
                        "id": "com.example/orders#Order.Export@exportJson",
                        "operationId": "exportJson",
                        "method": "GET",
                        "path": "/orders.json",
                        "inputSchema": {"type": "object"},
                    },
                ],
            },
        ],
    }


class CapabilityGraphTests(unittest.TestCase):
    def test_loads_and_resolves_single_binding_action_alias(self) -> None:
        graph = CapabilityGraph.from_dict(graph_dict())
        action, binding = graph.resolve("com.example/orders#Order.List")
        self.assertEqual(action.name, "List")
        self.assertEqual(binding.operation_id, "listOrders")

    def test_loads_graph_from_json_and_file(self) -> None:
        text = json.dumps(graph_dict())
        self.assertEqual(
            CapabilityGraph.from_json(text).package.id, "com.example/orders"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability-graph.json"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(
                CapabilityGraph.from_file(path).actions[0].name,
                "List",
            )

    def test_requires_binding_id_for_multi_binding_action(self) -> None:
        graph = CapabilityGraph.from_dict(graph_dict())
        with self.assertRaises(AmbiguousActionError):
            graph.resolve("com.example/orders#Order.Export")

    def test_rejects_binding_outside_action_namespace(self) -> None:
        value = graph_dict()
        value["actions"][0]["bindings"][0]["id"] = (
            "com.example/other#Order.List@listOrders"
        )
        with self.assertRaises(CapabilityGraphError):
            CapabilityGraph.from_dict(value)

    def test_rejects_noncanonical_action_id(self) -> None:
        value = graph_dict()
        value["actions"][0]["id"] = "com.example/orders#Order.Search"
        value["actions"][0]["bindings"][0]["id"] = (
            "com.example/orders#Order.Search@listOrders"
        )
        with self.assertRaises(CapabilityGraphError):
            CapabilityGraph.from_dict(value)

    def test_rejects_invalid_package_id(self) -> None:
        value = graph_dict()
        value["package"]["id"] = "orders"
        with self.assertRaises(CapabilityGraphError):
            CapabilityGraph.from_dict(value)

    def test_rejects_invalid_http_binding(self) -> None:
        value = graph_dict()
        value["actions"][0]["bindings"][0]["method"] = "CONNECT"
        with self.assertRaises(CapabilityGraphError):
            CapabilityGraph.from_dict(value)

    def test_rejects_binding_id_that_disagrees_with_operation(self) -> None:
        value = graph_dict()
        value["actions"][0]["bindings"][0]["id"] = (
            "com.example/orders#Order.List@anotherOperation"
        )
        with self.assertRaises(CapabilityGraphError):
            CapabilityGraph.from_dict(value)

    def test_graph_models_are_immutable(self) -> None:
        graph = CapabilityGraph.from_dict(graph_dict())
        schema = graph.actions[0].bindings[0].input_schema
        with self.assertRaises(TypeError):
            schema["type"] = "string"

    def test_ui_capability_round_trips_with_graph(self) -> None:
        value = graph_dict()
        value["ui"] = {
            "mode": "UIComponent",
            "rootComponent": "todo-board",
            "kind": "sandboxed",
            "entry": "components/todo-board/main.tsx",
            "surfaces": ["app-page", "chat"],
        }
        graph = CapabilityGraph.from_dict(value)
        app = TangramApp.from_graph(graph)

        self.assertEqual(app.ui()["rootComponent"], "todo-board")
        self.assertEqual(
            CapabilityGraph.from_json(graph.to_json()).to_dict(), graph.to_dict()
        )


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.graph = CapabilityGraph.from_dict(graph_dict())
        self.driver = InMemoryDriver()
        self.audit = MemoryAuditSink()

        @self.driver.handler("com.example/orders#Order.List@listOrders")
        async def list_orders(arguments):
            return {"orders": [], "status": arguments.get("status")}

        @self.driver.handler("com.example/orders#Order.Approve@approveOrder")
        async def approve(arguments):
            return {"approved": arguments["orderId"]}

    async def test_stateless_action_is_allowed_and_audited_without_payload(
        self,
    ) -> None:
        host = TangramHost(self.graph, driver=self.driver, audit=self.audit)
        result = await host.call("com.example/orders#Order.List", {"status": "pending"})
        self.assertEqual(result, {"orders": [], "status": "pending"})
        self.assertEqual(self.audit.events[0].outcome, "success")
        self.assertTrue(self.audit.events[0].arguments_hash.startswith("sha256:"))
        self.assertFalse(hasattr(self.audit.events[0], "arguments"))

    async def test_input_is_validated_before_driver_or_policy(self) -> None:
        host = TangramHost(self.graph, driver=self.driver, audit=self.audit)
        with self.assertRaises(InputValidationError):
            await host.call("com.example/orders#Order.List", {"unexpected": True})
        self.assertEqual(self.audit.events[0].outcome, "invalid_input")
        self.assertEqual(self.audit.events[0].decision, "not_evaluated")

    async def test_output_schema_is_enforced_and_failure_is_audited(self) -> None:
        driver = InMemoryDriver()

        @driver.handler("com.example/orders#Order.List@listOrders")
        async def invalid_output(arguments):
            return "not an object"

        host = TangramHost(self.graph, driver=driver, audit=self.audit)
        with self.assertRaises(OutputValidationError):
            await host.call("com.example/orders#Order.List", {})
        self.assertEqual(self.audit.events[0].outcome, "error")

    async def test_required_and_numeric_constraints_are_enforced(self) -> None:
        host = TangramHost(self.graph, driver=self.driver, audit=self.audit)
        with self.assertRaises(InputValidationError):
            await host.call("com.example/orders#Order.Approve", {})
        with self.assertRaises(InputValidationError):
            await host.call("com.example/orders#Order.Approve", {"orderId": 0})

    async def test_mutation_is_denied_by_default(self) -> None:
        host = TangramHost(self.graph, driver=self.driver, audit=self.audit)
        with self.assertRaises(PolicyDeniedError):
            await host.call("com.example/orders#Order.Approve", {"orderId": 1})
        self.assertEqual(self.audit.events[0].decision, "deny")

    async def test_allowed_mutation_still_requires_confirmation(self) -> None:
        host = TangramHost(
            self.graph,
            driver=self.driver,
            policy=LocalDevelopmentPolicy(
                allow_mutations={"com.example/orders#Order.Approve"}
            ),
            audit=self.audit,
        )
        with self.assertRaises(ConfirmationRequiredError):
            await host.call("com.example/orders#Order.Approve", {"orderId": 1})

    async def test_explicit_startup_preauthorization_allows_mutation(self) -> None:
        action_id = "com.example/orders#Order.Approve"
        host = TangramHost(
            self.graph,
            driver=self.driver,
            policy=LocalDevelopmentPolicy(
                allow_mutations={action_id},
                preauthorized_confirmations={action_id},
            ),
            audit=self.audit,
        )
        result = await host.call(action_id, {"orderId": 7})
        self.assertEqual(result, {"approved": 7})


if __name__ == "__main__":
    unittest.main()
