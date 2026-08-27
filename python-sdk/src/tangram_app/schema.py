"""A strict JSON Schema subset used for agent-facing invocation inputs.

The capability compiler is expected to dereference OpenAPI schemas. This
validator intentionally rejects unsupported keywords that change validation
semantics, instead of silently accepting inputs under a weaker contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any

from .errors import InputValidationError


_SUPPORTED = {
    "$schema",
    "$id",
    "title",
    "description",
    "default",
    "example",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "allOf",
    "anyOf",
    "oneOf",
    "nullable",
    "format",
}


def validate(instance: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    unsupported = sorted(set(schema) - _SUPPORTED)
    if unsupported:
        raise InputValidationError(
            path, f"unsupported schema keywords: {', '.join(unsupported)}"
        )

    if schema.get("nullable") is True and instance is None:
        return
    if "const" in schema and instance != schema["const"]:
        raise InputValidationError(path, f"must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise InputValidationError(path, f"must be one of {schema['enum']!r}")

    for child in schema.get("allOf", []):
        validate(instance, _schema(child, path), path)
    if "anyOf" in schema:
        if not _matching(instance, schema["anyOf"], path):
            raise InputValidationError(path, "does not match any allowed schema")
    if "oneOf" in schema:
        matches = _matching(instance, schema["oneOf"], path)
        if len(matches) != 1:
            raise InputValidationError(
                path, f"must match exactly one schema; matched {len(matches)}"
            )

    expected = schema.get("type")
    if _is_sequence(expected):
        if not any(_is_type(instance, item) for item in expected):
            raise InputValidationError(path, f"must have one of types {expected!r}")
    elif expected is not None and not _is_type(instance, expected):
        raise InputValidationError(path, f"must be of type {expected}")

    if isinstance(instance, Mapping):
        _validate_object(instance, schema, path)
    elif isinstance(instance, Sequence) and not isinstance(
        instance, (str, bytes, bytearray)
    ):
        _validate_array(instance, schema, path)
    elif isinstance(instance, str):
        _validate_string(instance, schema, path)
    elif _is_number(instance):
        _validate_number(instance, schema, path)


def _schema(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputValidationError(path, "schema must be an object")
    return value


def _matching(instance: Any, choices: Any, path: str) -> list[int]:
    if not _is_sequence(choices):
        raise InputValidationError(path, "schema alternatives must be an array")
    matches: list[int] = []
    for index, choice in enumerate(choices):
        try:
            validate(instance, _schema(choice, path), path)
            matches.append(index)
        except InputValidationError:
            pass
    return matches


def _is_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "object": isinstance(value, Mapping),
        "array": isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray)),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": _is_number(value),
    }.get(expected, False)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _validate_object(
    instance: Mapping[str, Any], schema: Mapping[str, Any], path: str
) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise InputValidationError(path, "properties must be an object")
    required = schema.get("required", [])
    if not _is_sequence(required) or not all(
        isinstance(item, str) for item in required
    ):
        raise InputValidationError(path, "required must be an array of strings")
    for name in required:
        if name not in instance:
            raise InputValidationError(path, f"missing required property {name!r}")
    for name, value in instance.items():
        child_path = f"{path}.{name}"
        if name in properties:
            validate(value, _schema(properties[name], child_path), child_path)
        elif schema.get("additionalProperties") is False:
            raise InputValidationError(child_path, "additional property is not allowed")
        elif isinstance(schema.get("additionalProperties"), Mapping):
            validate(value, schema["additionalProperties"], child_path)


def _validate_array(
    instance: Sequence[Any], schema: Mapping[str, Any], path: str
) -> None:
    if "minItems" in schema and len(instance) < schema["minItems"]:
        raise InputValidationError(
            path, f"must contain at least {schema['minItems']} items"
        )
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        raise InputValidationError(
            path, f"must contain at most {schema['maxItems']} items"
        )
    if "items" in schema:
        item_schema = _schema(schema["items"], path)
        for index, value in enumerate(instance):
            validate(value, item_schema, f"{path}[{index}]")


def _validate_string(instance: str, schema: Mapping[str, Any], path: str) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        raise InputValidationError(
            path, f"must be at least {schema['minLength']} characters"
        )
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        raise InputValidationError(
            path, f"must be at most {schema['maxLength']} characters"
        )
    if "pattern" in schema and re.search(schema["pattern"], instance) is None:
        raise InputValidationError(path, f"must match pattern {schema['pattern']!r}")


def _validate_number(
    instance: int | float, schema: Mapping[str, Any], path: str
) -> None:
    checks = (
        ("minimum", lambda value: instance >= value, "greater than or equal to"),
        ("maximum", lambda value: instance <= value, "less than or equal to"),
        ("exclusiveMinimum", lambda value: instance > value, "greater than"),
        ("exclusiveMaximum", lambda value: instance < value, "less than"),
    )
    for keyword, predicate, phrase in checks:
        if keyword in schema and not predicate(schema[keyword]):
            raise InputValidationError(path, f"must be {phrase} {schema[keyword]}")
    if "multipleOf" in schema:
        divisor = schema["multipleOf"]
        if divisor <= 0 or not math.isclose(
            instance / divisor, round(instance / divisor)
        ):
            raise InputValidationError(path, f"must be a multiple of {divisor}")
