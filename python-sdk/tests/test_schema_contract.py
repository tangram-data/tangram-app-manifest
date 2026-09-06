"""Regression tests for OpenAPI -> graph -> host -> HTTP contract preservation."""

from __future__ import annotations

import json
import unittest

from tangram_app import (
    CapabilityGraph,
    InMemoryDriver,
    InputValidationError,
    ManifestCompilationError,
    TangramHost,
)
from tangram_app.compiler import _compile_input, _normalize_schema, _Operation
from tangram_app.http_driver import OpenApiRequestRenderer
from tangram_app.schema import validate


def body_graph(schema, *, required=True, parameters=(), version="3.1.0"):
    operation = _Operation(
        "test",
        "POST",
        "/test",
        {},
        {
            "parameters": list(parameters),
            "requestBody": {
                "required": required,
                "content": {
                    "application/json": {"schema": schema},
                },
            },
        },
    )
    inputs, bindings, body_required = _compile_input(
        {"openapi": version},
        operation,
    )
    graph = CapabilityGraph.from_dict(
        {
            "formatVersion": "1",
            "manifestSpecVersion": "v1",
            "package": {"id": "test/app", "version": "1", "digest": "sha256:test"},
            "actions": [
                {
                    "id": "test/app#Test.Call",
                    "resourceType": "Test",
                    "name": "Call",
                    "description": "Test",
                    "effect": "Stateless",
                    "idempotent": True,
                    "bindings": [
                        {
                            "id": "test/app#Test.Call@test",
                            "operationId": "test",
                            "method": "POST",
                            "path": "/test",
                            "inputSchema": inputs,
                            "inputBindings": bindings,
                            "bodyRequired": body_required,
                        }
                    ],
                }
            ],
        }
    )
    # Exercise the persisted snapshot, including frozen schemas, not just dicts.
    return CapabilityGraph.from_json(graph.to_json())


class BodyContractTests(unittest.IsolatedAsyncioTestCase):
    async def call_body(self, graph, args):
        driver = InMemoryDriver()
        binding = graph.actions[0].bindings[0]

        @driver.handler(binding.id)
        async def handler(arguments):
            request = OpenApiRequestRenderer("http://127.0.0.1").render(
                binding,
                arguments,
            )
            return None if request.body is None else json.loads(request.body)

        return await TangramHost(graph, driver=driver).call(binding.id, args)

    async def test_composition_rejects_invalid_body_before_driver(self):
        for keyword in ("oneOf", "allOf"):
            with self.subTest(keyword=keyword):
                graph = body_graph(
                    {
                        "type": "object",
                        "properties": {
                            "a": {"type": "string"},
                            "b": {"type": "string"},
                        },
                        keyword: (
                            [{"required": ["a"]}, {"required": ["b"]}]
                            if keyword == "oneOf"
                            else [{"required": ["a"]}]
                        ),
                    }
                )
                invalid = {"a": "x", "b": "y"} if keyword == "oneOf" else {"b": "y"}
                with self.assertRaises(InputValidationError):
                    await self.call_body(graph, {"body": invalid})
                self.assertEqual(
                    await self.call_body(graph, {"body": {"a": "x"}}), {"a": "x"}
                )

    async def test_optional_body_distinguishes_omission_empty_and_valid(self):
        graph = body_graph(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            required=False,
        )
        self.assertIsNone(await self.call_body(graph, {}))
        with self.assertRaises(InputValidationError):
            await self.call_body(graph, {"body": {}})
        self.assertEqual(await self.call_body(graph, {"body": {"a": "x"}}), {"a": "x"})

    async def test_open_object_preserves_extra_properties(self):
        graph = body_graph({"type": "object", "properties": {"a": {"type": "string"}}})
        value = {"a": "x", "extra": [1, 2]}
        self.assertEqual(await self.call_body(graph, {"body": value}), value)

    async def test_typed_additional_properties_are_validated(self):
        graph = body_graph(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": {"type": "integer"},
            }
        )
        self.assertEqual(
            await self.call_body(graph, {"body": {"extra": 2}}), {"extra": 2}
        )
        with self.assertRaises(InputValidationError):
            await self.call_body(graph, {"body": {"extra": "bad"}})

    async def test_safe_closed_required_body_still_flattens_with_collisions(self):
        graph = body_graph(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            parameters=[{"name": "a", "in": "query", "schema": {"type": "string"}}],
        )
        self.assertEqual(
            await self.call_body(graph, {"body_a": "x", "query_a": "y"}), {"a": "x"}
        )
        with self.assertRaises(InputValidationError):
            await self.call_body(graph, {"body_a": "x", "extra": 1})

    async def test_required_empty_body_and_whole_body_name_collision(self):
        graph = body_graph(
            {"type": "object"},
            parameters=[
                {
                    "name": "body",
                    "in": "query",
                    "schema": {"type": "string"},
                }
            ],
        )
        with self.assertRaises(InputValidationError):
            await self.call_body(graph, {})
        self.assertEqual(
            await self.call_body(graph, {"body_body": {}, "query_body": "x"}), {}
        )

    async def test_nullable_body_accepts_explicit_null(self):
        graph = body_graph(
            {
                "type": "object",
                "nullable": True,
                "properties": {"a": {"type": "string"}},
                "additionalProperties": False,
            },
            version="3.0.3",
        )
        self.assertIsNone(await self.call_body(graph, {"body": None}))


class SchemaDialectTests(unittest.TestCase):
    def normalize(self, schema, version="3.0.3", components=None):
        return _normalize_schema(
            {"openapi": version, "components": components or {}}, schema, "test", set()
        )

    def test_openapi30_boolean_exclusive_bounds(self):
        for exclusive, inclusive, valid, invalid in (
            ("exclusiveMinimum", "minimum", 0.5, 0),
            ("exclusiveMaximum", "maximum", -0.5, 0),
        ):
            with self.subTest(exclusive=exclusive):
                schema = self.normalize(
                    {"type": "number", inclusive: 0, exclusive: True}
                )
                validate(valid, schema)
                with self.assertRaises(InputValidationError):
                    validate(invalid, schema)
                validate(
                    0,
                    self.normalize({"type": "number", inclusive: 0, exclusive: False}),
                )

    def test_openapi31_numeric_bounds(self):
        schema = self.normalize({"type": "number", "exclusiveMinimum": 0}, "3.1.0")
        validate(0.5, schema)
        with self.assertRaises(InputValidationError):
            validate(0, schema)

    def test_wrong_dialect_bound_types_fail_during_compilation(self):
        for version, bound in (("3.0.3", 1), ("3.1.0", True)):
            with self.subTest(version=version):
                with self.assertRaises(ManifestCompilationError):
                    self.normalize({"minimum": 0, "exclusiveMinimum": bound}, version)

    def test_nullable_preserves_enum_and_composition_constraints(self):
        validate(None, self.normalize({"type": "string", "nullable": True}))
        for extra in ({"enum": ["x"]}, {"allOf": [{"type": "string"}]}):
            schema = self.normalize({"type": "string", "nullable": True, **extra})
            with self.assertRaises(InputValidationError):
                validate(None, schema)
        validate(
            None,
            self.normalize({"type": "string", "nullable": True, "enum": ["x", None]}),
        )

    def test_reference_siblings_follow_dialect(self):
        components = {"schemas": {"Base": {"type": "number", "maximum": 5}}}
        value = {"$ref": "#/components/schemas/Base", "maximum": 10}
        for version in ("3.0.3", "3.1.0"):
            schema = self.normalize(value, version, components)
            validate(4, schema)
            with self.assertRaises(InputValidationError):
                validate(8, schema)
        value = {"$ref": "#/components/schemas/Base", "minimum": 3}
        validate(1, self.normalize(value, "3.0.3", components))
        with self.assertRaises(InputValidationError):
            validate(1, self.normalize(value, "3.1.0", components))

    def test_reference_sibling_can_reuse_same_ref_without_recursion(self):
        components = {"schemas": {"Base": {"type": "string"}}}
        schema = self.normalize(
            {
                "$ref": "#/components/schemas/Base",
                "allOf": [{"$ref": "#/components/schemas/Base"}],
            },
            "3.1.0",
            components,
        )
        validate("x", schema)

    def test_json_equality_distinguishes_nested_booleans_from_numbers(self):
        for keyword in ("enum", "const"):
            for expected, invalid in (
                (1, True),
                (False, 0),
                ([1], [True]),
                ({"a": 1}, {"a": True}),
            ):
                with self.subTest(keyword=keyword, expected=expected):
                    schema = {keyword: [expected] if keyword == "enum" else expected}
                    validate(expected, schema)
                    with self.assertRaises(InputValidationError):
                        validate(invalid, schema)
        validate(1.0, {"enum": [1]})

    def test_unknown_dialects_fail_closed(self):
        with self.assertRaises(ManifestCompilationError):
            self.normalize({"type": "string"}, "4.0.0")
        with self.assertRaises(ManifestCompilationError):
            self.normalize(
                {"$schema": "https://example.com/custom", "type": "string"}, "3.1.0"
            )


class UiProjectionTests(unittest.TestCase):
    def project(self, graph, value):
        from tangram_app.local_ui import _flatten_action_arguments

        return _flatten_action_arguments(value, graph.actions[0].bindings[0])

    def test_whole_body_preserves_null_empty_and_extra_fields(self):
        graph = body_graph({"type": ["object", "null"]}, required=False)
        for body in (None, {}, {"extra": 1}):
            with self.subTest(body=body):
                self.assertEqual(
                    self.project(graph, {"requestBody": body}), {"body": body}
                )
        self.assertEqual(self.project(graph, {}), {})

    def test_ui_envelope_uses_collision_mapping_for_flat_body(self):
        graph = body_graph(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": False,
            },
            parameters=[{"name": "a", "in": "query", "schema": {"type": "string"}}],
        )
        self.assertEqual(
            self.project(
                graph,
                {
                    "parameters": {"a": "query"},
                    "requestBody": {"a": "body"},
                },
            ),
            {"query_a": "query", "body_a": "body"},
        )
        with self.assertRaises(ValueError):
            self.project(graph, {"requestBody": {"extra": 1}})

    def test_direct_arguments_are_not_reprojected(self):
        graph = body_graph({"type": "object"})
        self.assertEqual(self.project(graph, {"body": {"a": 1}}), {"body": {"a": 1}})

    def test_envelope_cannot_overwrite_direct_arguments(self):
        graph = body_graph({"type": "object"})
        with self.assertRaises(ValueError):
            self.project(graph, {"body": {"a": 1}, "requestBody": {"a": 2}})


class ReviewRegressionTests(unittest.TestCase):
    def test_legacy_nullable_graph_still_checks_other_constraints(self):
        validate(None, {"type": "string", "nullable": True})
        for constraints in (
            {"enum": ["x"]},
            {"const": "x"},
            {"allOf": [{"type": "string"}]},
            {"oneOf": [{"type": "null"}, {"type": "null"}]},
        ):
            with self.subTest(constraints=constraints):
                with self.assertRaises(InputValidationError):
                    validate(None, {"type": "string", "nullable": True, **constraints})

    def test_malformed_required_rejected_at_compile_time(self):
        for required in ("ab", None, 1, [1]):
            with self.subTest(required=required):
                with self.assertRaisesRegex(
                    ManifestCompilationError, "required must be an array"
                ):
                    body_graph(
                        {
                            "type": "object",
                            "properties": {"a": {}, "b": {}},
                            "required": required,
                            "additionalProperties": False,
                        }
                    )
