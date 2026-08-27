"""Python mirrors of the public Tangram manifest domain.

Names intentionally follow the Scala manifest-facing classes in
``ai.tangram.os.Application``. The Python fields use snake_case while decoders
consume the canonical camelCase JSON emitted by Pkl.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .errors import ManifestDecodeError


JsonObject = Mapping[str, Any]


class ApplicationType(str, Enum):
    APP = "App"
    CONNECTOR = "Connector"
    AGENT = "Agent"


class ActionEffect(str, Enum):
    STATELESS = "Stateless"
    REVERSIBLE = "Reversible"
    IRREVERSIBLE = "Irreversible"


class CloudProvider(str, Enum):
    LOCAL = "Local"
    AWS = "AWS"
    AZURE = "Azure"
    GCP = "GCP"
    SCALEWAY = "Scaleway"
    OVH = "Ovh"


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestDecodeError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ManifestDecodeError(f"{path} must be an array")
    return value


def _string(value: Any, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ManifestDecodeError(f"{path} must be a string")
    return value


def _boolean(value: Any, path: str, *, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise ManifestDecodeError(f"{path} must be a boolean")
    return value


def _strings(value: Any, path: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{path}[{index}]")  # type: ignore[arg-type]
        for index, item in enumerate(_array(value, path))
    )


def _optional_strings(value: Any, path: str) -> tuple[str, ...] | None:
    return None if value is None else _strings(value, path)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Application:
    manifest_spec_version: str
    group: str
    name: str
    version: str
    app_type: ApplicationType
    description: str | None = None
    readme: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    license: str | None = None
    provider_website: str | None = None
    website: str | None = None
    supported_cloud_providers: tuple[CloudProvider, ...] | None = None

    @property
    def id(self) -> str:
        return f"{self.group}/{self.name}"

    @classmethod
    def from_dict(cls, value: Any, path: str = "app.pkl") -> "Application":
        obj = _object(value, path)
        try:
            app_type = ApplicationType(_string(obj.get("appType"), f"{path}.appType"))
        except ValueError as error:
            raise ManifestDecodeError(
                f"{path}.appType must be App, Connector, or Agent"
            ) from error
        raw_clouds = obj.get("supportedCloudProviders")
        try:
            clouds = (
                None
                if raw_clouds is None
                else tuple(
                    CloudProvider(
                        _string(item, f"{path}.supportedCloudProviders[{index}]")
                    )
                    for index, item in enumerate(
                        _array(raw_clouds, f"{path}.supportedCloudProviders")
                    )
                )
            )
        except ValueError as error:
            raise ManifestDecodeError(
                f"{path}.supportedCloudProviders is invalid"
            ) from error
        return cls(
            manifest_spec_version=_string(
                obj.get("manifestSpecVersion"), f"{path}.manifestSpecVersion"
            ),  # type: ignore[arg-type]
            group=_string(obj.get("group"), f"{path}.group"),  # type: ignore[arg-type]
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
            version=_string(obj.get("version"), f"{path}.version"),  # type: ignore[arg-type]
            app_type=app_type,
            description=_string(
                obj.get("description"), f"{path}.description", optional=True
            ),
            readme=_string(obj.get("readme"), f"{path}.readme", optional=True),
            category=_string(obj.get("category"), f"{path}.category", optional=True),
            tags=()
            if obj.get("tags") is None
            else _strings(obj["tags"], f"{path}.tags"),
            license=_string(obj.get("license"), f"{path}.license", optional=True),
            provider_website=_string(
                obj.get("providerWebsite"), f"{path}.providerWebsite", optional=True
            ),
            website=_string(obj.get("website"), f"{path}.website", optional=True),
            supported_cloud_providers=clouds,
        )


@dataclass(frozen=True, slots=True)
class ConfigField:
    name: str
    description: str
    required: bool
    default: str | None = None
    example: str | None = None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ConfigField":
        obj = _object(value, path)
        return cls(
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
            description=_string(obj.get("description"), f"{path}.description"),  # type: ignore[arg-type]
            required=_boolean(obj.get("required"), f"{path}.required"),  # type: ignore[arg-type]
            default=_string(obj.get("default"), f"{path}.default", optional=True),
            example=_string(obj.get("example"), f"{path}.example", optional=True),
        )


@dataclass(frozen=True, slots=True)
class AppName:
    group: str
    name: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "AppName":
        obj = _object(value, path)
        return cls(
            group=_string(obj.get("group"), f"{path}.group"),  # type: ignore[arg-type]
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ResourceType:
    app: AppName
    name: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ResourceType":
        obj = _object(value, path)
        return cls(
            app=AppName.from_dict(obj.get("app"), f"{path}.app"),
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
        )


class ResourceNameExtractor:
    @staticmethod
    def from_dict(value: Any, path: str) -> "ResourceNameExtractor":
        obj = _object(value, path)
        kind = obj.get("type")
        if kind is None and "name" in obj:
            kind = "PathParam"
        if kind == "PathParam":
            return PathParam(_string(obj.get("name"), f"{path}.name"))  # type: ignore[arg-type]
        if kind == "QueryParam":
            return QueryParam(_string(obj.get("name"), f"{path}.name"))  # type: ignore[arg-type]
        if kind == "BodyField":
            return BodyField(_string(obj.get("path"), f"{path}.path"))  # type: ignore[arg-type]
        if kind == "ResponseField":
            return ResponseField(_string(obj.get("path"), f"{path}.path"))  # type: ignore[arg-type]
        raise ManifestDecodeError(f"{path}.type has unknown extractor {kind!r}")


@dataclass(frozen=True, slots=True)
class PathParam(ResourceNameExtractor):
    name: str


@dataclass(frozen=True, slots=True)
class QueryParam(ResourceNameExtractor):
    name: str


@dataclass(frozen=True, slots=True)
class BodyField(ResourceNameExtractor):
    path: str


@dataclass(frozen=True, slots=True)
class ResponseField(ResourceNameExtractor):
    path: str


@dataclass(frozen=True, slots=True)
class OpenApiMapping:
    operation_id: str
    resource_name: tuple[ResourceNameExtractor, ...] | None = None
    resource_id: ResourceNameExtractor | None = None
    new_name: ResourceNameExtractor | None = None
    resource_name_template: str | None = None
    resource_id_template: str | None = None
    new_name_template: str | None = None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "OpenApiMapping":
        obj = _object(value, path)
        raw_resource_name = obj.get("resourceName")
        return cls(
            operation_id=_string(obj.get("operationId"), f"{path}.operationId"),  # type: ignore[arg-type]
            resource_name=(
                None
                if raw_resource_name is None
                else tuple(
                    ResourceNameExtractor.from_dict(
                        item, f"{path}.resourceName[{index}]"
                    )
                    for index, item in enumerate(
                        _array(raw_resource_name, f"{path}.resourceName")
                    )
                )
            ),
            resource_id=(
                None
                if obj.get("resourceId") is None
                else ResourceNameExtractor.from_dict(
                    obj["resourceId"], f"{path}.resourceId"
                )
            ),
            new_name=(
                None
                if obj.get("newName") is None
                else ResourceNameExtractor.from_dict(obj["newName"], f"{path}.newName")
            ),
            resource_name_template=_string(
                obj.get("resourceNameTemplate"),
                f"{path}.resourceNameTemplate",
                optional=True,
            ),
            resource_id_template=_string(
                obj.get("resourceIdTemplate"),
                f"{path}.resourceIdTemplate",
                optional=True,
            ),
            new_name_template=_string(
                obj.get("newNameTemplate"), f"{path}.newNameTemplate", optional=True
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourcePrivilegeRequirement:
    resource_type: str
    privilege: str
    resource_name: tuple[ResourceNameExtractor, ...] | None = None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ResourcePrivilegeRequirement":
        obj = _object(value, path)
        raw_names = obj.get("resourceName")
        return cls(
            resource_type=_string(obj.get("resourceType"), f"{path}.resourceType"),  # type: ignore[arg-type]
            privilege=_string(obj.get("privilege"), f"{path}.privilege"),  # type: ignore[arg-type]
            resource_name=(
                None
                if raw_names is None
                else tuple(
                    ResourceNameExtractor.from_dict(
                        item, f"{path}.resourceName[{index}]"
                    )
                    for index, item in enumerate(
                        _array(raw_names, f"{path}.resourceName")
                    )
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionPresentation:
    kind: str
    value: JsonObject

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ActionPresentation":
        obj = _object(value, path)
        return cls(
            kind=_string(obj.get("kind"), f"{path}.kind"),  # type: ignore[arg-type]
            value=_freeze(obj),
        )


_WELL_KNOWN_PRIVILEGES = {
    "Describe",
    "Read",
    "Write",
    "Create",
    "Delete",
    "Admin",
    "Execute",
}


@dataclass(frozen=True, slots=True)
class ResourceTypeAction:
    name: str
    doc: str | None
    skip_audit: bool | None
    skip_auth: bool | None
    requires_confirmation: bool | None
    privilege: str | None
    effect: ActionEffect | None
    idempotent: bool | None
    additional_privileges: tuple[ResourcePrivilegeRequirement, ...]
    open_api_mapping: OpenApiMapping | None
    open_api_mappings: tuple[OpenApiMapping, ...]
    presentation: ActionPresentation | None

    @property
    def effective_privilege(self) -> str | None:
        if self.privilege is not None:
            return self.privilege
        return self.name if self.name in _WELL_KNOWN_PRIVILEGES else None

    @property
    def all_open_api_mappings(self) -> tuple[OpenApiMapping, ...]:
        prefix = () if self.open_api_mapping is None else (self.open_api_mapping,)
        return prefix + self.open_api_mappings

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ResourceTypeAction":
        obj = _object(value, path)
        raw_effect = obj.get("effect")
        try:
            effect = None if raw_effect is None else ActionEffect(raw_effect)
        except ValueError as error:
            raise ManifestDecodeError(f"{path}.effect is invalid") from error
        raw_additional = obj.get("additionalPrivileges") or []
        raw_mappings = obj.get("openApiMappings") or []
        return cls(
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
            doc=_string(obj.get("doc"), f"{path}.doc", optional=True),
            skip_audit=_boolean(
                obj.get("skipAudit"), f"{path}.skipAudit", optional=True
            ),
            skip_auth=_boolean(obj.get("skipAuth"), f"{path}.skipAuth", optional=True),
            requires_confirmation=_boolean(
                obj.get("requiresConfirmation"),
                f"{path}.requiresConfirmation",
                optional=True,
            ),
            privilege=_string(obj.get("privilege"), f"{path}.privilege", optional=True),
            effect=effect,
            idempotent=_boolean(
                obj.get("idempotent"), f"{path}.idempotent", optional=True
            ),
            additional_privileges=tuple(
                ResourcePrivilegeRequirement.from_dict(
                    item, f"{path}.additionalPrivileges[{index}]"
                )
                for index, item in enumerate(
                    _array(raw_additional, f"{path}.additionalPrivileges")
                )
            ),
            open_api_mapping=(
                None
                if obj.get("openApiMapping") is None
                else OpenApiMapping.from_dict(
                    obj["openApiMapping"], f"{path}.openApiMapping"
                )
            ),
            open_api_mappings=tuple(
                OpenApiMapping.from_dict(item, f"{path}.openApiMappings[{index}]")
                for index, item in enumerate(
                    _array(raw_mappings, f"{path}.openApiMappings")
                )
            ),
            presentation=(
                None
                if obj.get("presentation") is None
                else ActionPresentation.from_dict(
                    obj["presentation"], f"{path}.presentation"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceRole:
    name: str
    permissions: tuple[str, ...]
    description: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "ResourceRole":
        obj = _object(value, path)
        return cls(
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
            permissions=_strings(obj.get("permissions"), f"{path}.permissions"),
            description=_string(obj.get("description"), f"{path}.description"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AppResourceTypeVersion:
    version: str
    served: bool
    super_type_version: str | None
    actions: tuple[ResourceTypeAction, ...]
    preset_roles: tuple[ResourceRole, ...]

    def action(self, name: str) -> ResourceTypeAction:
        for action in self.actions:
            if action.name == name:
                return action
        raise LookupError(
            f"unknown action {name!r} in resource version {self.version!r}"
        )

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "AppResourceTypeVersion":
        obj = _object(value, path)
        raw_actions = obj.get("actions") or []
        raw_roles = obj.get("presetRoles") or []
        return cls(
            version=_string(obj.get("version"), f"{path}.version"),  # type: ignore[arg-type]
            served=_boolean(obj.get("served"), f"{path}.served"),  # type: ignore[arg-type]
            super_type_version=_string(
                obj.get("superTypeVersion"), f"{path}.superTypeVersion", optional=True
            ),
            actions=tuple(
                ResourceTypeAction.from_dict(item, f"{path}.actions[{index}]")
                for index, item in enumerate(_array(raw_actions, f"{path}.actions"))
            ),
            preset_roles=tuple(
                ResourceRole.from_dict(item, f"{path}.presetRoles[{index}]")
                for index, item in enumerate(_array(raw_roles, f"{path}.presetRoles"))
            ),
        )


@dataclass(frozen=True, slots=True)
class AppResourceTypeDefinition:
    name: str
    active_version: str
    versions: tuple[AppResourceTypeVersion, ...]
    super_type: ResourceType | None
    scope_type: ResourceType | None
    doc: str | None
    scope_privilege_propagation: Mapping[str, tuple[str, ...]] | None = None

    @property
    def active(self) -> AppResourceTypeVersion:
        for version in self.versions:
            if version.version == self.active_version:
                return version
        raise ManifestDecodeError(
            f"resource type {self.name!r} activeVersion {self.active_version!r} does not exist"
        )

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "AppResourceTypeDefinition":
        obj = _object(value, path)
        raw_versions = obj.get("versions") or []
        raw_propagation = obj.get("scopePrivilegePropagation")
        propagation = (
            None
            if raw_propagation is None
            else MappingProxyType(
                {
                    str(key): _strings(items, f"{path}.scopePrivilegePropagation.{key}")
                    for key, items in _object(
                        raw_propagation, f"{path}.scopePrivilegePropagation"
                    ).items()
                }
            )
        )
        return cls(
            name=_string(obj.get("name"), f"{path}.name"),  # type: ignore[arg-type]
            active_version=_string(obj.get("activeVersion"), f"{path}.activeVersion"),  # type: ignore[arg-type]
            versions=tuple(
                AppResourceTypeVersion.from_dict(item, f"{path}.versions[{index}]")
                for index, item in enumerate(_array(raw_versions, f"{path}.versions"))
            ),
            super_type=(
                None
                if obj.get("superType") is None
                else ResourceType.from_dict(obj["superType"], f"{path}.superType")
            ),
            scope_type=(
                None
                if obj.get("scopeType") is None
                else ResourceType.from_dict(obj["scopeType"], f"{path}.scopeType")
            ),
            doc=_string(obj.get("doc"), f"{path}.doc", optional=True),
            scope_privilege_propagation=propagation,
        )


@dataclass(frozen=True, slots=True)
class ManifestPackage:
    application: Application
    resource_type_definitions: tuple[AppResourceTypeDefinition, ...]
    settings: tuple[ConfigField, ...]
    secrets: tuple[ConfigField, ...]
    api_spec: JsonObject | None
    agent_spec: JsonObject | None
    ui_spec: JsonObject | None
    source_root: Path

    def resource_type(self, name: str) -> AppResourceTypeDefinition:
        for resource_type in self.resource_type_definitions:
            if resource_type.name == name:
                return resource_type
        raise LookupError(f"unknown resource type {name!r}")


# Scala's package loader calls the app.pkl projection AppInfo. Application is
# the friendlier public Python name; AppInfo remains available for parity.
AppInfo = Application
