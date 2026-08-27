"""Immutable models for the versioned Tangram Capability Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from .errors import AmbiguousActionError, CapabilityGraphError, UnknownBindingError


JsonObject = Mapping[str, Any]


class CapabilityEffect(str, Enum):
    STATELESS = "Stateless"
    REVERSIBLE = "Reversible"
    IRREVERSIBLE = "Irreversible"


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityGraphError(f"{path} must be an object")
    return value


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "a non-empty string" if nonempty else "a string"
        raise CapabilityGraphError(f"{path} must be {qualifier}")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise CapabilityGraphError(f"{path} must be a boolean")
    return value


def _string_sequence(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CapabilityGraphError(f"{path} must be an array of strings")
    return tuple(_string(item, f"{path}[{index}]") for index, item in enumerate(value))


def _frozen_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _frozen_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    id: str
    version: str
    digest: str

    @classmethod
    def from_dict(cls, value: Any, path: str = "package") -> "PackageIdentity":
        obj = _object(value, path)
        package = cls(
            id=_string(obj.get("id"), f"{path}.id"),
            version=_string(obj.get("version"), f"{path}.version"),
            digest=_string(obj.get("digest"), f"{path}.digest"),
        )
        parts = package.id.split("/")
        if len(parts) != 2 or not all(parts):
            raise CapabilityGraphError(f"{path}.id must have the form group/name")
        return package

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class InputBinding:
    location: str
    name: str | None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "InputBinding":
        obj = _object(value, path)
        location = _string(obj.get("location"), f"{path}.location")
        if location not in {"path", "query", "header", "body"}:
            raise CapabilityGraphError(
                f"{path}.location must be path, query, header, or body"
            )
        raw_name = obj.get("name")
        name = None if raw_name is None else _string(raw_name, f"{path}.name")
        if location != "body" and name is None:
            raise CapabilityGraphError(f"{path}.name is required for {location} inputs")
        return cls(location=location, name=name)

    def to_dict(self) -> dict[str, str]:
        value = {"location": self.location}
        if self.name is not None:
            value["name"] = self.name
        return value


@dataclass(frozen=True, slots=True)
class OperationBinding:
    id: str
    operation_id: str
    method: str
    path: str
    input_schema: JsonObject
    output_schema: JsonObject | None = None
    body_required: bool = False
    input_bindings: Mapping[str, InputBinding] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "OperationBinding":
        obj = _object(value, path)
        raw_output = obj.get("outputSchema")
        raw_input_bindings = obj.get("inputBindings", {})
        if not isinstance(raw_input_bindings, Mapping):
            raise CapabilityGraphError(f"{path}.inputBindings must be an object")
        binding = cls(
            id=_string(obj.get("id"), f"{path}.id"),
            operation_id=_string(obj.get("operationId"), f"{path}.operationId"),
            method=_string(obj.get("method"), f"{path}.method").upper(),
            path=_string(obj.get("path"), f"{path}.path"),
            input_schema=_frozen_json(
                _object(obj.get("inputSchema"), f"{path}.inputSchema")
            ),
            output_schema=(
                None
                if raw_output is None
                else _frozen_json(_object(raw_output, f"{path}.outputSchema"))
            ),
            body_required=_boolean(
                obj.get("bodyRequired", False), f"{path}.bodyRequired"
            ),
            input_bindings=MappingProxyType(
                {
                    _string(name, f"{path}.inputBindings key"): InputBinding.from_dict(
                        item, f"{path}.inputBindings.{name}"
                    )
                    for name, item in raw_input_bindings.items()
                }
            ),
        )
        allowed_methods = {
            "GET",
            "PUT",
            "POST",
            "DELETE",
            "OPTIONS",
            "HEAD",
            "PATCH",
            "TRACE",
        }
        if binding.method not in allowed_methods:
            raise CapabilityGraphError(f"{path}.method is not an OpenAPI HTTP method")
        if not binding.path.startswith("/"):
            raise CapabilityGraphError(f"{path}.path must start with '/'")
        if _has_path_traversal(binding.path):
            raise CapabilityGraphError(f"{path}.path contains a traversal segment")
        return binding

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "operationId": self.operation_id,
            "method": self.method,
            "path": self.path,
            "inputSchema": _plain_json(self.input_schema),
        }
        if self.output_schema is not None:
            value["outputSchema"] = _plain_json(self.output_schema)
        if self.body_required:
            value["bodyRequired"] = True
        if self.input_bindings:
            value["inputBindings"] = {
                name: binding.to_dict() for name, binding in self.input_bindings.items()
            }
        return value


@dataclass(frozen=True, slots=True)
class ActionCapability:
    id: str
    resource_type: str
    name: str
    description: str
    effect: CapabilityEffect
    idempotent: bool
    requires_confirmation: bool
    required_privileges: tuple[str, ...]
    bindings: tuple[OperationBinding, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ActionCapability":
        obj = _object(value, path)
        raw_bindings = obj.get("bindings")
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise CapabilityGraphError(f"{path}.bindings must be a non-empty array")
        try:
            effect = CapabilityEffect(_string(obj.get("effect"), f"{path}.effect"))
        except ValueError as error:
            raise CapabilityGraphError(
                f"{path}.effect must be Stateless, Reversible, or Irreversible"
            ) from error
        action = cls(
            id=_string(obj.get("id"), f"{path}.id"),
            resource_type=_string(obj.get("resourceType"), f"{path}.resourceType"),
            name=_string(obj.get("name"), f"{path}.name"),
            description=_string(
                obj.get("description"), f"{path}.description", nonempty=False
            ),
            effect=effect,
            idempotent=_boolean(obj.get("idempotent"), f"{path}.idempotent"),
            requires_confirmation=_boolean(
                obj.get("requiresConfirmation", False), f"{path}.requiresConfirmation"
            ),
            required_privileges=_string_sequence(
                obj.get("requiredPrivileges", []), f"{path}.requiredPrivileges"
            ),
            bindings=tuple(
                OperationBinding.from_dict(binding, f"{path}.bindings[{index}]")
                for index, binding in enumerate(raw_bindings)
            ),
        )
        expected_prefix = f"{action.id}@"
        for binding in action.bindings:
            if not binding.id.startswith(expected_prefix):
                raise CapabilityGraphError(
                    f"binding id {binding.id!r} must start with {expected_prefix!r}"
                )
        return action

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resourceType": self.resource_type,
            "name": self.name,
            "description": self.description,
            "effect": self.effect.value,
            "idempotent": self.idempotent,
            "requiresConfirmation": self.requires_confirmation,
            "requiredPrivileges": list(self.required_privileges),
            "bindings": [binding.to_dict() for binding in self.bindings],
        }


@dataclass(frozen=True, slots=True)
class CapabilityGraph:
    format_version: str
    manifest_spec_version: str
    package: PackageIdentity
    actions: tuple[ActionCapability, ...]
    runtime_requirements: JsonObject = field(
        default_factory=lambda: MappingProxyType({})
    )
    authority: str = "unknown"
    development_only: bool = False
    ui: JsonObject = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityGraph":
        obj = _object(value, "graph")
        format_version = _string(obj.get("formatVersion"), "formatVersion")
        if format_version != "1":
            raise CapabilityGraphError(
                f"unsupported capability graph formatVersion {format_version!r}; expected '1'"
            )
        raw_actions = obj.get("actions")
        if not isinstance(raw_actions, list):
            raise CapabilityGraphError("actions must be an array")
        authority = _string(obj.get("authority", "unknown"), "authority")
        if authority not in {"unknown", "development", "publishing"}:
            raise CapabilityGraphError(
                "authority must be 'unknown', 'development', or 'publishing'"
            )
        graph = cls(
            format_version=format_version,
            manifest_spec_version=_string(
                obj.get("manifestSpecVersion"), "manifestSpecVersion"
            ),
            package=PackageIdentity.from_dict(obj.get("package")),
            actions=tuple(
                ActionCapability.from_dict(action, f"actions[{index}]")
                for index, action in enumerate(raw_actions)
            ),
            runtime_requirements=_frozen_json(
                _object(obj.get("runtimeRequirements", {}), "runtimeRequirements")
            ),
            authority=authority,
            development_only=_boolean(
                obj.get("developmentOnly", False), "developmentOnly"
            ),
            ui=_frozen_json(_object(obj.get("ui", {}), "ui")),
        )
        graph._validate_contract()
        return graph

    @classmethod
    def from_json(cls, text: str) -> "CapabilityGraph":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise CapabilityGraphError(
                f"invalid capability graph JSON: {error}"
            ) from error
        return cls.from_dict(value)

    @classmethod
    def from_file(cls, path: str | Path) -> "CapabilityGraph":
        graph_path = Path(path)
        try:
            text = graph_path.read_text(encoding="utf-8")
        except OSError as error:
            raise CapabilityGraphError(
                f"could not read {graph_path}: {error}"
            ) from error
        return cls.from_json(text)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "formatVersion": self.format_version,
            "manifestSpecVersion": self.manifest_spec_version,
            "package": self.package.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
        }
        if self.runtime_requirements:
            value["runtimeRequirements"] = _plain_json(self.runtime_requirements)
        if self.authority != "unknown":
            value["authority"] = self.authority
        if self.development_only:
            value["developmentOnly"] = True
        if self.ui:
            value["ui"] = _plain_json(self.ui)
        return value

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            indent=indent,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
        ) + ("" if indent is None else "\n")

    def write_file(self, path: str | Path) -> None:
        graph_path = Path(path)
        try:
            graph_path.parent.mkdir(parents=True, exist_ok=True)
            graph_path.write_text(self.to_json(), encoding="utf-8")
        except OSError as error:
            raise CapabilityGraphError(
                f"could not write {graph_path}: {error}"
            ) from error

    def _validate_contract(self) -> None:
        action_ids: set[str] = set()
        binding_ids: set[str] = set()
        for action in self.actions:
            expected_action_id = (
                f"{self.package.id}#{action.resource_type}.{action.name}"
            )
            if action.id != expected_action_id:
                raise CapabilityGraphError(
                    f"action id {action.id!r} must equal {expected_action_id!r}"
                )
            if action.id in action_ids:
                raise CapabilityGraphError(f"duplicate action id {action.id!r}")
            action_ids.add(action.id)
            for binding in action.bindings:
                expected_binding_id = f"{action.id}@{binding.operation_id}"
                if binding.id != expected_binding_id:
                    raise CapabilityGraphError(
                        f"binding id {binding.id!r} must equal {expected_binding_id!r}"
                    )
                if binding.id in binding_ids:
                    raise CapabilityGraphError(f"duplicate binding id {binding.id!r}")
                binding_ids.add(binding.id)

    def resolve(self, id: str) -> tuple[ActionCapability, OperationBinding]:
        """Resolve a binding id, or an action id when it has exactly one binding."""
        action_match: ActionCapability | None = None
        for action in self.actions:
            if action.id == id:
                action_match = action
            for binding in action.bindings:
                if binding.id == id:
                    return action, binding
        if action_match is None:
            raise UnknownBindingError(f"unknown action binding {id!r}")
        if len(action_match.bindings) != 1:
            choices = ", ".join(binding.id for binding in action_match.bindings)
            raise AmbiguousActionError(f"{id!r} has multiple bindings: {choices}")
        return action_match, action_match.bindings[0]


def _has_path_traversal(value: str) -> bool:
    """Reject literal and commonly encoded traversal before HTTP normalization."""
    decoded = value
    for _ in range(8):
        if any(
            segment in {".", ".."} for segment in decoded.replace("\\", "/").split("/")
        ):
            return True
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        # Excessive encoding is ambiguous across HTTP stacks and has no useful
        # place in a static OpenAPI operation path.
        return True
    return False


# Compatibility aliases from the graph-only 0.1 draft. New code should use
# ActionCapability and OperationBinding so manifest definitions are not
# confused with their compiled, executable projection.
Effect = CapabilityEffect
ActionBinding = OperationBinding
Action = ActionCapability
