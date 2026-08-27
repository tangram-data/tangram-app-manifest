"""Compile validated Tangram manifests into the versioned capability graph."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ManifestCompilationError
from .manifest import ActionEffect, ApplicationType, ManifestPackage, ResourceTypeAction
from .models import CapabilityGraph
from .validation import ValidationFinding, validate_manifest


_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_WRITE_PRIVILEGES = {"Write", "Create", "Delete", "Admin", "Execute"}
_PREFERRED_SUCCESS_CODES = ("200", "201", "202", "203", "206")
_SCHEMA_KEYWORDS = {
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


@dataclass(frozen=True, slots=True)
class CompilationResult:
    graph: CapabilityGraph
    findings: tuple[ValidationFinding, ...]


@dataclass(frozen=True, slots=True)
class _Operation:
    operation_id: str
    method: str
    path: str
    path_item: Mapping[str, Any]
    value: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _Input:
    location: str
    name: str | None
    schema: Mapping[str, Any]
    required: bool


class ManifestCompiler:
    """Compile the executable projection of a manifest package.

    Validation remains the conformance gate. The compiler includes only active
    resource-version actions that carry one or more OpenAPI mappings.
    """

    def compile(self, package_root: str | Path) -> CompilationResult:
        root = Path(package_root).resolve()
        validation = validate_manifest(root)
        if not validation.valid:
            detail = "; ".join(
                f"{finding.path}: {finding.message}" for finding in validation.errors
            )
            raise ManifestCompilationError(detail or "manifest validation failed")
        package = validation.require_valid()
        document = self._load_openapi(package)
        operations = self._index_operations(document)
        graph_value = {
            "formatVersion": "1",
            "authority": "development",
            "developmentOnly": _has_local_project_dependencies(root),
            "manifestSpecVersion": package.application.manifest_spec_version,
            "package": {
                "id": package.application.id,
                "version": package.application.version,
                "digest": _package_digest(root),
            },
            "actions": self._compile_actions(package, document, operations),
            "runtimeRequirements": self._runtime_requirements(package),
            "ui": _ui_capability(package),
        }
        return CompilationResult(
            graph=CapabilityGraph.from_dict(graph_value),
            findings=validation.findings,
        )

    def _load_openapi(self, package: ManifestPackage) -> Mapping[str, Any]:
        api = package.api_spec
        if api is None:
            if _has_mapped_actions(package):
                raise ManifestCompilationError(
                    "mapped actions require manifests/api/spec.pkl"
                )
            return {"openapi": "3.0.0", "paths": {}}
        spec_file = api.get("apiSpecFile")
        if not isinstance(spec_file, str) or not spec_file:
            if _has_mapped_actions(package):
                raise ManifestCompilationError("mapped actions require apiSpecFile")
            return {"openapi": "3.0.0", "paths": {}}
        path = package.source_root / "manifests/api" / spec_file
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ManifestCompilationError(f"could not read {path}: {error}") from error
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml
            except ModuleNotFoundError as error:
                raise ManifestCompilationError(
                    "PyYAML is required to compile OpenAPI YAML"
                ) from error
            try:
                value = yaml.safe_load(text)
            except yaml.YAMLError as error:
                raise ManifestCompilationError(
                    f"invalid OpenAPI YAML: {error}"
                ) from error
        if not isinstance(value, Mapping):
            raise ManifestCompilationError("OpenAPI document must be an object")
        return value

    def _index_operations(self, document: Mapping[str, Any]) -> dict[str, _Operation]:
        paths = document.get("paths", {})
        if not isinstance(paths, Mapping):
            raise ManifestCompilationError("OpenAPI paths must be an object")
        operations: dict[str, _Operation] = {}
        for raw_path, raw_item in paths.items():
            if not isinstance(raw_path, str) or not isinstance(raw_item, Mapping):
                continue
            path_item = _resolve_object(document, raw_item, f"paths.{raw_path}")
            for method, raw_operation in path_item.items():
                if str(method).lower() not in _HTTP_METHODS or not isinstance(
                    raw_operation, Mapping
                ):
                    continue
                operation = _resolve_object(
                    document, raw_operation, f"{str(method).upper()} {raw_path}"
                )
                operation_id = operation.get("operationId")
                if not isinstance(operation_id, str) or not operation_id:
                    continue
                if operation_id in operations:
                    raise ManifestCompilationError(
                        f"duplicate OpenAPI operationId {operation_id!r}"
                    )
                operations[operation_id] = _Operation(
                    operation_id=operation_id,
                    method=str(method).upper(),
                    path=raw_path,
                    path_item=path_item,
                    value=operation,
                )
        return operations

    def _compile_actions(
        self,
        package: ManifestPackage,
        document: Mapping[str, Any],
        operations: Mapping[str, _Operation],
    ) -> list[dict[str, Any]]:
        compiled: list[dict[str, Any]] = []
        for resource_type in package.resource_type_definitions:
            try:
                version = resource_type.active
            except Exception as error:
                raise ManifestCompilationError(str(error)) from error
            for action in version.actions:
                mappings = action.all_open_api_mappings
                if not mappings:
                    continue
                if action.effect is None or action.idempotent is None:
                    raise ManifestCompilationError(
                        f"{resource_type.name}.{action.name} lacks effect or idempotent"
                    )
                action_id = (
                    f"{package.application.id}#{resource_type.name}.{action.name}"
                )
                bindings: list[dict[str, Any]] = []
                for mapping in mappings:
                    operation = operations.get(mapping.operation_id)
                    if operation is None:
                        raise ManifestCompilationError(
                            f"operationId {mapping.operation_id!r} was not found"
                        )
                    input_schema, input_bindings, body_required = _compile_input(
                        document, operation
                    )
                    binding: dict[str, Any] = {
                        "id": f"{action_id}@{operation.operation_id}",
                        "operationId": operation.operation_id,
                        "method": operation.method,
                        "path": operation.path,
                        "inputSchema": input_schema,
                        "inputBindings": input_bindings,
                    }
                    if body_required:
                        binding["bodyRequired"] = True
                    output_schema = _compile_output(document, operation)
                    if output_schema is not None:
                        binding["outputSchema"] = output_schema
                    bindings.append(binding)
                compiled.append(
                    {
                        "id": action_id,
                        "resourceType": resource_type.name,
                        "name": action.name,
                        "description": action.doc or "",
                        "effect": action.effect.value,
                        "idempotent": action.idempotent,
                        "requiresConfirmation": _requires_confirmation(action),
                        "requiredPrivileges": _required_privileges(
                            resource_type.name, action
                        ),
                        "bindings": bindings,
                    }
                )
        return compiled

    def _runtime_requirements(self, package: ManifestPackage) -> dict[str, Any]:
        backend = {
            ApplicationType.APP: "service",
            ApplicationType.CONNECTOR: "connector",
            ApplicationType.AGENT: "agent",
        }[package.application.app_type]
        infrastructure_claims = []
        if (package.source_root / "manifests/deployment/dependencies.pkl").is_file():
            infrastructure_claims.append(
                {
                    "name": "deployment/dependencies.pkl",
                    "required": True,
                }
            )
        return {
            "backend": backend,
            "settings": [
                {"name": item.name, "required": item.required}
                for item in package.settings
            ],
            "secrets": [
                {"name": item.name, "required": item.required}
                for item in package.secrets
            ],
            "infrastructureClaims": infrastructure_claims,
        }


def compile_manifest(package_root: str | Path) -> CompilationResult:
    """Validate and compile a manifest package into its executable graph."""
    return ManifestCompiler().compile(package_root)


def _has_mapped_actions(package: ManifestPackage) -> bool:
    return any(
        action.all_open_api_mappings
        for resource_type in package.resource_type_definitions
        for version in resource_type.versions
        for action in version.actions
    )


def _has_local_project_dependencies(root: Path) -> bool:
    lock_path = root / "manifests/PklProject.deps.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    resolved = lock.get("resolvedDependencies", {})
    return isinstance(resolved, Mapping) and any(
        isinstance(item, Mapping) and item.get("type") == "local"
        for item in resolved.values()
    )


def _ui_capability(package: ManifestPackage) -> dict[str, Any]:
    ui = package.ui_spec
    if not isinstance(ui, Mapping):
        return {}
    deployment = ui.get("deployment")
    mode = deployment.get("mode") if isinstance(deployment, Mapping) else None
    root_name = (
        deployment.get("rootComponent") if isinstance(deployment, Mapping) else None
    )
    value: dict[str, Any] = {}
    if isinstance(mode, str):
        value["mode"] = mode
    if isinstance(root_name, str):
        value["rootComponent"] = root_name
    components = ui.get("components", ())
    if isinstance(components, Sequence) and not isinstance(components, str):
        selected = next(
            (
                item
                for item in components
                if isinstance(item, Mapping)
                and (not isinstance(root_name, str) or item.get("name") == root_name)
            ),
            None,
        )
        if isinstance(selected, Mapping):
            for source, target in (
                ("name", "name"),
                ("kind", "kind"),
                ("description", "description"),
                ("entry", "entry"),
                ("surfaces", "surfaces"),
                ("spec", "spec"),
            ):
                if source in selected:
                    value[target] = _plain_value(selected[source])
    return value


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return value


def _required_privileges(resource_type: str, action: ResourceTypeAction) -> list[str]:
    primary = action.effective_privilege or "Write"
    values = [f"{resource_type}:{primary}"]
    values.extend(
        f"{requirement.resource_type}:{requirement.privilege}"
        for requirement in action.additional_privileges
    )
    return list(dict.fromkeys(values))


def _requires_confirmation(action: ResourceTypeAction) -> bool:
    if action.effect is ActionEffect.IRREVERSIBLE:
        return True
    if action.requires_confirmation is not None:
        return action.requires_confirmation
    return action.effect is ActionEffect.REVERSIBLE or (
        action.effective_privilege in _WRITE_PRIVILEGES
    )


def _compile_input(
    document: Mapping[str, Any], operation: _Operation
) -> tuple[dict[str, Any], dict[str, dict[str, str]], bool]:
    inputs: list[_Input] = []
    body_required = False
    parameters: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_parameters in (
        operation.path_item.get("parameters", []),
        operation.value.get("parameters", []),
    ):
        if not isinstance(raw_parameters, Sequence) or isinstance(raw_parameters, str):
            continue
        for raw_parameter in raw_parameters:
            if not isinstance(raw_parameter, Mapping):
                continue
            parameter = _resolve_object(document, raw_parameter, operation.operation_id)
            location, name = parameter.get("in"), parameter.get("name")
            if location == "cookie":
                raise ManifestCompilationError(
                    f"{operation.operation_id} uses unsupported cookie parameter {name!r}"
                )
            if location in {"path", "query", "header"} and isinstance(name, str):
                parameters[(location, name)] = parameter
    managed_headers = _managed_header_names(document)
    for (location, name), parameter in parameters.items():
        if location == "header" and name.lower() in managed_headers:
            continue
        raw_schema = parameter.get("schema")
        if not isinstance(raw_schema, Mapping):
            raise ManifestCompilationError(
                f"{operation.operation_id}.{location}.{name} requires a schema"
            )
        schema = _normalize_schema(
            document, raw_schema, f"{operation.operation_id}.{location}.{name}", set()
        )
        inputs.append(
            _Input(
                location=location,
                name=name,
                schema=schema,
                required=location == "path" or parameter.get("required") is True,
            )
        )

    raw_body = operation.value.get("requestBody")
    if isinstance(raw_body, Mapping):
        body = _resolve_object(
            document, raw_body, f"{operation.operation_id}.requestBody"
        )
        body_required = body.get("required") is True
        media = _json_media_type(body.get("content"))
        if media is None and body.get("content"):
            raise ManifestCompilationError(
                f"{operation.operation_id} requestBody requires JSON media content"
            )
        if media is not None and isinstance(media.get("schema"), Mapping):
            body_schema = _normalize_schema(
                document,
                media["schema"],
                f"{operation.operation_id}.requestBody",
                set(),
            )
            properties = body_schema.get("properties")
            if (
                isinstance(properties, Mapping)
                and properties
                and (body_schema.get("type") == "object" or "type" not in body_schema)
            ):
                required = body_schema.get("required", [])
                required_names = set(required) if isinstance(required, list) else set()
                for name, schema in properties.items():
                    if isinstance(name, str) and isinstance(schema, Mapping):
                        inputs.append(
                            _Input("body", name, schema, name in required_names)
                        )
            else:
                inputs.append(_Input("body", None, body_schema, body_required))
        elif media is not None:
            raise ManifestCompilationError(
                f"{operation.operation_id} requestBody JSON content requires a schema"
            )

    counts = Counter(item.name if item.name is not None else "body" for item in inputs)
    used: set[str] = set()
    properties: dict[str, Any] = {}
    required: list[str] = []
    bindings: dict[str, dict[str, str]] = {}
    for item in inputs:
        natural = item.name if item.name is not None else "body"
        candidate = natural if counts[natural] == 1 else f"{item.location}_{natural}"
        exposed = _unique_name(candidate, used)
        used.add(exposed)
        properties[exposed] = dict(item.schema)
        if item.required:
            required.append(exposed)
        binding = {"location": item.location}
        if item.name is not None:
            binding["name"] = item.name
        bindings[exposed] = binding
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema, bindings, body_required


def _compile_output(
    document: Mapping[str, Any], operation: _Operation
) -> dict[str, Any] | None:
    raw_responses = operation.value.get("responses")
    if not isinstance(raw_responses, Mapping):
        return None
    responses = {str(code): value for code, value in raw_responses.items()}
    response: Mapping[str, Any] | None = None
    for code in _PREFERRED_SUCCESS_CODES:
        candidate = responses.get(code)
        if isinstance(candidate, Mapping):
            response = candidate
            break
    if response is None:
        for code in sorted(responses):
            candidate = responses.get(code)
            if code.startswith("2") and isinstance(candidate, Mapping):
                response = candidate
                break
    if response is None:
        candidate = responses.get("default")
        if isinstance(candidate, Mapping):
            response = candidate
    if response is None:
        return None
    response = _resolve_object(document, response, f"{operation.operation_id}.response")
    media = _json_media_type(response.get("content"))
    if media is None or not isinstance(media.get("schema"), Mapping):
        return None
    return _normalize_schema(
        document, media["schema"], f"{operation.operation_id}.response", set()
    )


def _json_media_type(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    for name in ("application/json", "application/*+json"):
        media = value.get(name)
        if isinstance(media, Mapping):
            return media
    for name, media in value.items():
        if (
            isinstance(name, str)
            and "json" in name.lower()
            and isinstance(media, Mapping)
        ):
            return media
    return None


def _managed_header_names(document: Mapping[str, Any]) -> set[str]:
    names = {"authorization", "proxy-authorization"}
    components = document.get("components", {})
    schemes = (
        components.get("securitySchemes", {}) if isinstance(components, Mapping) else {}
    )
    if isinstance(schemes, Mapping):
        for raw_scheme in schemes.values():
            if not isinstance(raw_scheme, Mapping):
                continue
            scheme = _resolve_object(document, raw_scheme, "securityScheme")
            if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
                name = scheme.get("name")
                if isinstance(name, str):
                    names.add(name.lower())
    return names


def _normalize_schema(
    document: Mapping[str, Any],
    value: Any,
    path: str,
    seen: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestCompilationError(f"{path} schema must be an object")
    raw_reference = value.get("$ref")
    if isinstance(raw_reference, str):
        if raw_reference in seen:
            raise ManifestCompilationError(
                f"recursive schema reference {raw_reference!r}"
            )
        target = _resolve_pointer(document, raw_reference, path)
        merged = dict(target)
        merged.update({key: item for key, item in value.items() if key != "$ref"})
        return _normalize_schema(document, merged, path, seen | {raw_reference})
    unsupported = sorted(set(value) - _SCHEMA_KEYWORDS)
    if unsupported:
        raise ManifestCompilationError(
            f"{path} uses unsupported schema keyword(s): {', '.join(unsupported)}"
        )
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "properties":
            if not isinstance(item, Mapping):
                raise ManifestCompilationError(f"{path}.properties must be an object")
            result[key] = {
                str(name): _normalize_schema(
                    document, child, f"{path}.properties.{name}", seen
                )
                for name, child in item.items()
            }
        elif key in {"items", "additionalProperties"} and isinstance(item, Mapping):
            result[key] = _normalize_schema(document, item, f"{path}.{key}", seen)
        elif key in {"allOf", "anyOf", "oneOf"}:
            if not isinstance(item, Sequence) or isinstance(item, str):
                raise ManifestCompilationError(f"{path}.{key} must be an array")
            result[key] = [
                _normalize_schema(document, child, f"{path}.{key}[{index}]", seen)
                for index, child in enumerate(item)
            ]
        else:
            result[key] = item
    return result


def _resolve_object(
    document: Mapping[str, Any], value: Mapping[str, Any], path: str
) -> Mapping[str, Any]:
    reference = value.get("$ref")
    if not isinstance(reference, str):
        return value
    target = _resolve_pointer(document, reference, path)
    merged = dict(target)
    merged.update({key: item for key, item in value.items() if key != "$ref"})
    return merged


def _resolve_pointer(
    document: Mapping[str, Any], reference: str, path: str
) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ManifestCompilationError(
            f"{path} uses unsupported external ref {reference!r}"
        )
    current: Any = document
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise ManifestCompilationError(f"{path} cannot resolve ref {reference!r}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise ManifestCompilationError(f"{path} ref {reference!r} is not an object")
    return current


def _unique_name(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    return f"{candidate}_{index}"


def _package_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in ("manifests", "libs"):
        directory = root / name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ManifestCompilationError(
                    f"package digest does not allow symlink {path.relative_to(root)}"
                )
            if path.is_file():
                files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ManifestCompilationError(f"could not hash {path}: {error}") from error
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"
