"""OpenAPI parameter serialization survives compilation and graph persistence."""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from tangram_app import (
    CapabilityGraphError,
    ManifestCompilationError,
    OpenApiRequestRenderer,
)
from tangram_app.compiler import _compile_input, _Operation
from tangram_app.models import ActionBinding, InputBinding


def compile_parameter(location="query", schema=None, *, path_item=None, **options):
    parameter = {
        "in": location,
        "name": "ids",
        "schema": schema or {"type": "array", "items": {"type": "string"}},
        **options,
    }
    path = "/test/{ids}" if location == "path" else "/test"
    operation = _Operation(
        "test", "GET", path, path_item or {}, {"parameters": [parameter]}
    )
    inputs, bindings, required = _compile_input({"openapi": "3.1.0"}, operation)
    binding = ActionBinding.from_dict(
        {
            "id": "test/app#Test.Call@test",
            "operationId": "test",
            "method": "GET",
            "path": path,
            "inputSchema": inputs,
            "inputBindings": bindings,
            "bodyRequired": required,
        },
        "binding",
    )
    return ActionBinding.from_dict(json.loads(json.dumps(binding.to_dict())), "binding")


class ParameterSerializationTests(unittest.TestCase):
    def render(self, binding, value):
        return OpenApiRequestRenderer("http://127.0.0.1").render(
            binding, {"ids": value}
        )

    def test_query_form_explode_false_round_trips_and_joins(self):
        binding = compile_parameter(style="form", explode=False)
        source = binding.input_bindings["ids"]
        self.assertEqual(source.style, "form")
        self.assertIs(source.explode, False)
        self.assertEqual(
            urlsplit(self.render(binding, ["a", "b"]).url).query, "ids=a,b"
        )

    def test_query_default_and_explicit_exploded_arrays(self):
        for options in ({}, {"style": "form"}, {"explode": True}):
            binding = compile_parameter(**options)
            self.assertIs(binding.input_bindings["ids"].explode, True)
            self.assertEqual(
                parse_qs(urlsplit(self.render(binding, ["a", "b"]).url).query),
                {"ids": ["a", "b"]},
            )

    def test_escaping_does_not_turn_data_into_delimiters(self):
        binding = compile_parameter(explode=False)
        query = urlsplit(self.render(binding, ["a,b", "c&d", "x y", "ü"]).url).query
        self.assertEqual(query, "ids=a%2Cb,c%26d,x%20y,%C3%BC")
        exploded = self.render(compile_parameter(), ["a,b", "c&d", "x y"])
        self.assertEqual(
            parse_qs(urlsplit(exploded.url).query), {"ids": ["a,b", "c&d", "x y"]}
        )

    def test_path_and_header_simple_arrays_with_either_explode(self):
        for explode in (False, True):
            path = self.render(
                compile_parameter("path", style="simple", explode=explode),
                ["a,b", "c d"],
            )
            self.assertEqual(urlsplit(path.url).path, "/test/a%2Cb,c%20d")
            header = self.render(
                compile_parameter("header", style="simple", explode=explode), ["a", "b"]
            )
            self.assertEqual(header.headers["ids"], "a,b")

    def test_scalar_values_and_query_injection(self):
        for value, schema, expected in (
            (True, {"type": "boolean"}, "true"),
            (12, {"type": "integer"}, "12"),
            ("a&admin=true", {"type": "string"}, "a&admin=true"),
        ):
            for explode in (False, True):
                query = urlsplit(
                    self.render(
                        compile_parameter(schema=schema, explode=explode), value
                    ).url
                ).query
                self.assertEqual(parse_qs(query), {"ids": [expected]})

    def test_operation_parameter_override_preserves_its_serialization(self):
        parent = {
            "parameters": [
                {
                    "in": "query",
                    "name": "ids",
                    "explode": False,
                    "schema": {"type": "string"},
                }
            ]
        }
        binding = compile_parameter(path_item=parent)
        self.assertIs(binding.input_bindings["ids"].explode, True)

    def test_unsupported_styles_fail_at_compilation_and_graph_load(self):
        for location, style in (
            ("query", "deepObject"),
            ("query", "pipeDelimited"),
            ("query", "spaceDelimited"),
            ("path", "matrix"),
            ("path", "label"),
            ("header", "form"),
        ):
            with self.subTest(location=location, style=style):
                with self.assertRaisesRegex(
                    ManifestCompilationError, "unsupported.*style"
                ):
                    compile_parameter(location, style=style)
                with self.assertRaises(CapabilityGraphError):
                    InputBinding.from_dict(
                        {"location": location, "name": "ids", "style": style}, "binding"
                    )

    def test_invalid_explode_and_reserved_values_fail_early(self):
        for options in (
            {"explode": "false"},
            {"explode": 0},
            {"explode": None},
            {"allowReserved": True},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ManifestCompilationError):
                    compile_parameter(**options)

    def test_objects_and_nested_arrays_fail_compilation(self):
        for schema in (
            {"type": "object"},
            {"type": ["object", "null"]},
            {"anyOf": [{"type": "string"}, {"type": "object"}]},
            {"type": "array", "items": {"type": "object"}},
            {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        ):
            for location in ("query", "path", "header"):
                with self.subTest(schema=schema, location=location):
                    with self.assertRaises(ManifestCompilationError):
                        compile_parameter(location, schema=schema)

    def test_legacy_bindings_keep_defaults_and_wire_shape(self):
        for location in ("query", "path", "header", "body"):
            value = {"location": location, "name": "ids"}
            binding = InputBinding.from_dict(value, "binding")
            self.assertEqual(binding.to_dict(), value)
            self.assertEqual(binding.effective_explode, location == "query")

    def test_body_cannot_declare_parameter_serialization(self):
        with self.assertRaises(CapabilityGraphError):
            InputBinding.from_dict({"location": "body", "style": "form"}, "binding")
