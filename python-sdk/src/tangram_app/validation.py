"""Structured, offline validation for Tangram manifest packages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from .errors import ManifestValidationError, TangramAppError
from .manifest import (
    ApplicationType,
    BodyField,
    ManifestPackage,
    PathParam,
    QueryParam,
    ResponseField,
)
from .pkl import PklManifestLoader


class Severity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    severity: Severity
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    package: ManifestPackage | None
    findings: tuple[ValidationFinding, ...]

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def valid(self) -> bool:
        return self.package is not None and not self.errors

    def require_valid(self) -> ManifestPackage:
        if not self.valid:
            summary = "; ".join(f"{f.path}: {f.message}" for f in self.errors)
            raise ManifestValidationError(summary or "manifest validation failed")
        assert self.package is not None
        return self.package


@dataclass(frozen=True, slots=True)
class OperationParameters:
    path: frozenset[str]
    query: frozenset[str]
    header: frozenset[str]
    cookie: frozenset[str]


class _Findings:
    def __init__(self) -> None:
        self.items: list[ValidationFinding] = []

    def add(self, severity: Severity, code: str, path: str, message: str) -> None:
        self.items.append(ValidationFinding(severity, code, path, message))

    def ok(self, code: str, path: str, message: str) -> None:
        self.add(Severity.OK, code, path, message)

    def warn(self, code: str, path: str, message: str) -> None:
        self.add(Severity.WARNING, code, path, message)

    def error(self, code: str, path: str, message: str) -> None:
        self.add(Severity.ERROR, code, path, message)


class ManifestLoader(Protocol):
    def load(self, package_root: str | Path) -> ManifestPackage: ...


def validate_manifest(
    package_root: str | Path,
    *,
    loader: ManifestLoader | None = None,
) -> ValidationResult:
    """Load and validate a package without contacting Tangram OS."""
    root = Path(package_root).resolve()
    manifests = root / "manifests"
    findings = _Findings()
    if not root.is_dir():
        findings.error("layout.package_root", ".", f"{root} is not a directory")
        return ValidationResult(None, tuple(findings.items))
    if not manifests.is_dir():
        findings.error("layout.manifests", "manifests/", "directory is missing")
        return ValidationResult(None, tuple(findings.items))

    removed = {
        "platform": "platform/ was removed; use an App with deployment/ and ui/spec.pkl",
        "deployment/spec.pkl": "deployment/spec.pkl was removed",
        "ui/components.pkl": "declare components in ui/spec.pkl",
        "deployment/infra_resources.pkl": "declare claims in deployment/dependencies.pkl",
        "integrations/implemented_integrations.pkl": "use per-file integration modules",
        "api/resource_types.pkl": "use api/resources.pkl",
    }
    for relative, message in removed.items():
        if (manifests / relative).exists():
            findings.error("layout.removed", f"manifests/{relative}", message)

    missing = False
    for relative in (
        "app.pkl",
        "PklProject",
        "PklProject.deps.json",
        "api/resources.pkl",
    ):
        if not (manifests / relative).is_file():
            missing = True
            findings.error(
                "layout.required_file",
                f"manifests/{relative}",
                "required file is missing",
            )
    if missing:
        return ValidationResult(None, tuple(findings.items))
    try:
        lock = json.loads(
            (manifests / "PklProject.deps.json").read_text(encoding="utf-8")
        )
        if not isinstance(lock, Mapping):
            raise ValueError("lock file must contain a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        findings.error(
            "layout.pkl_lock",
            "manifests/PklProject.deps.json",
            f"invalid dependency lock: {error}",
        )
        return ValidationResult(None, tuple(findings.items))
    _validate_dependency_lock(lock, findings)
    findings.ok("layout.valid", "manifests/", "required package layout is present")

    try:
        package = (loader or PklManifestLoader()).load(root)
    except (TangramAppError, OSError) as error:
        findings.error("pkl.load", "manifests/", str(error))
        return ValidationResult(None, tuple(findings.items))

    findings.ok(
        "application.loaded",
        "manifests/app.pkl",
        f"loaded {package.application.id}@{package.application.version}",
    )
    _validate_application(package, findings)
    _validate_configuration(package, findings)
    _validate_resource_types(package, findings)
    operations = _validate_api(package, manifests, findings)
    _validate_action_mappings(package, operations, findings)
    _validate_type_specific(package, manifests, findings)
    _validate_ui(package, manifests, findings)
    return ValidationResult(package, tuple(findings.items))


def _validate_dependency_lock(lock: Mapping[str, Any], findings: _Findings) -> None:
    resolved = lock.get("resolvedDependencies", {})
    if not isinstance(resolved, Mapping):
        findings.error(
            "layout.pkl_lock_dependencies",
            "manifests/PklProject.deps.json",
            "resolvedDependencies must be an object",
        )
        return
    for dependency, raw_value in resolved.items():
        path = f"manifests/PklProject.deps.json#resolvedDependencies.{dependency}"
        if not isinstance(raw_value, Mapping):
            findings.error(
                "layout.pkl_lock_dependency", path, "dependency entry must be an object"
            )
            continue
        dependency_type = raw_value.get("type")
        if dependency_type == "remote":
            uri = raw_value.get("uri")
            checksums = raw_value.get("checksums")
            checksum = (
                checksums.get("sha256") if isinstance(checksums, Mapping) else None
            )
            if not isinstance(uri, str) or not uri.startswith("projectpackage://"):
                findings.error(
                    "layout.pkl_lock_remote_uri",
                    path,
                    "remote dependency must have an exact projectpackage URI",
                )
            if (
                not isinstance(checksum, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", checksum) is None
            ):
                findings.error(
                    "layout.pkl_lock_remote_checksum",
                    path,
                    "remote dependency must have a SHA-256 checksum",
                )
        elif dependency_type == "local":
            if not isinstance(raw_value.get("path"), str):
                findings.error(
                    "layout.pkl_lock_local_path",
                    path,
                    "local dependency must declare a path",
                )
            else:
                findings.warn(
                    "layout.pkl_lock_local_development",
                    path,
                    "local project dependency makes this graph development-only",
                )
        else:
            findings.error(
                "layout.pkl_lock_dependency_type",
                path,
                "dependency type must be remote or local",
            )


def _validate_application(package: ManifestPackage, findings: _Findings) -> None:
    app = package.application
    if app.manifest_spec_version != "v1":
        findings.warn(
            "application.spec_version",
            "manifests/app.pkl#manifestSpecVersion",
            f"SDK currently targets v1; got {app.manifest_spec_version!r}",
        )
    if not app.group.strip() or not app.name.strip() or not app.version.strip():
        findings.error(
            "application.identity",
            "manifests/app.pkl",
            "group, name, and version must be non-empty",
        )
    if app.description is None or not app.description.strip():
        findings.warn(
            "application.description",
            "manifests/app.pkl#description",
            "description is empty; agents and catalog listings use it",
        )
    if app.category is None or not app.category.strip():
        findings.warn(
            "application.category",
            "manifests/app.pkl#category",
            "category is empty",
        )


def _validate_configuration(package: ManifestPackage, findings: _Findings) -> None:
    for label, fields in (("settings", package.settings), ("secrets", package.secrets)):
        names: set[str] = set()
        for index, field in enumerate(fields):
            path = f"manifests/{label}.pkl#{label}[{index}]"
            if not field.name.strip():
                findings.error("config.name", path, "field name must be non-empty")
            if field.name in names:
                findings.error(
                    "config.duplicate", path, f"duplicate field {field.name!r}"
                )
            names.add(field.name)
            if not field.description.strip():
                findings.error(
                    "config.description", path, "description must be non-empty"
                )


def _validate_resource_types(package: ManifestPackage, findings: _Findings) -> None:
    type_names: set[str] = set()
    for type_index, resource_type in enumerate(package.resource_type_definitions):
        base = f"manifests/api/resources.pkl#types[{type_index}]"
        if resource_type.name in type_names:
            findings.error(
                "resource_type.duplicate",
                base,
                f"duplicate type {resource_type.name!r}",
            )
        type_names.add(resource_type.name)
        if not resource_type.versions:
            findings.error("resource_type.versions", base, "no versions declared")
            continue
        version_names: set[str] = set()
        for version_index, version in enumerate(resource_type.versions):
            version_path = f"{base}.versions[{version_index}]"
            if version.version in version_names:
                findings.error(
                    "resource_version.duplicate",
                    version_path,
                    f"duplicate version {version.version!r}",
                )
            version_names.add(version.version)
            if not version.actions:
                findings.warn(
                    "resource_version.actions",
                    version_path,
                    "version declares no actions",
                )
            action_names: set[str] = set()
            for action_index, action in enumerate(version.actions):
                action_path = f"{version_path}.actions[{action_index}]"
                if action.name in action_names:
                    findings.error(
                        "action.duplicate",
                        action_path,
                        f"duplicate action {action.name!r}",
                    )
                action_names.add(action.name)
                if action.doc is None or not action.doc.strip():
                    findings.warn(
                        "action.doc",
                        action_path,
                        f"{resource_type.name}.{action.name} has an empty doc",
                    )
                if action.effect is None:
                    findings.error("action.effect", action_path, "effect is required")
                if action.idempotent is None:
                    findings.error(
                        "action.idempotent", action_path, "idempotent is required"
                    )
            role_names: set[str] = set()
            for role_index, role in enumerate(version.preset_roles):
                role_path = f"{version_path}.presetRoles[{role_index}]"
                if role.name in role_names:
                    findings.error(
                        "resource_role.duplicate",
                        role_path,
                        f"duplicate role {role.name!r}",
                    )
                role_names.add(role.name)
                # Permission tokens are intentionally open strings. Tangram
                # maps known action names to their effective privilege and
                # preserves every other token as an app-defined privilege.
        if resource_type.active_version not in version_names:
            findings.error(
                "resource_type.active_version",
                f"{base}.activeVersion",
                f"{resource_type.active_version!r} is not a declared version",
            )


def _validate_api(
    package: ManifestPackage, manifests: Path, findings: _Findings
) -> dict[str, OperationParameters]:
    api = package.api_spec
    has_mappings = any(
        action.all_open_api_mappings
        for resource_type in package.resource_type_definitions
        for version in resource_type.versions
        for action in version.actions
    )
    if api is None:
        if has_mappings:
            findings.error(
                "api.missing",
                "manifests/api/spec.pkl",
                "actions declare mappings but api/spec.pkl is missing",
            )
        return {}
    if package.application.app_type is ApplicationType.AGENT:
        findings.error(
            "agent.api_forbidden",
            "manifests/api/spec.pkl",
            "Agent packages may not declare api/spec.pkl",
        )
        return {}
    if package.application.app_type is ApplicationType.APP:
        backend = api.get("backend")
        if not isinstance(backend, Mapping):
            findings.error(
                "api.backend",
                "manifests/api/spec.pkl#backend",
                "App API specs require a ServiceBackend",
            )
        else:
            service_name, port = backend.get("serviceName"), backend.get("port")
            if not isinstance(service_name, str) or not service_name.strip():
                findings.error(
                    "api.backend_service",
                    "manifests/api/spec.pkl#backend.serviceName",
                    "serviceName must be non-empty",
                )
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                findings.error(
                    "api.backend_port",
                    "manifests/api/spec.pkl#backend.port",
                    "port must be between 1 and 65535",
                )
    if package.application.app_type is ApplicationType.CONNECTOR and not isinstance(
        api.get("auth"), Mapping
    ):
        findings.error(
            "connector.auth",
            "manifests/api/spec.pkl#auth",
            "Connector API specs require auth",
        )
    spec_file = api.get("apiSpecFile")
    if spec_file is None:
        if has_mappings:
            findings.error(
                "openapi.reference_missing",
                "manifests/api/spec.pkl#apiSpecFile",
                "mapped actions require apiSpecFile",
            )
        return {}
    if not isinstance(spec_file, str) or not spec_file:
        findings.error(
            "openapi.reference",
            "manifests/api/spec.pkl#apiSpecFile",
            "must be a file name",
        )
        return {}
    relative = Path(spec_file)
    if relative.is_absolute() or ".." in relative.parts:
        findings.error(
            "path.traversal",
            "manifests/api/spec.pkl#apiSpecFile",
            "must stay inside manifests/api/",
        )
        return {}
    api_dir = (manifests / "api").resolve()
    openapi_file = (api_dir / relative).resolve()
    if not openapi_file.is_relative_to(api_dir) or not openapi_file.is_file():
        findings.error(
            "openapi.file_missing",
            f"manifests/api/{spec_file}",
            "referenced OpenAPI document does not exist",
        )
        return {}
    try:
        document = _load_yaml_or_json(openapi_file)
    except (OSError, ValueError) as error:
        findings.error("openapi.parse", f"manifests/api/{spec_file}", str(error))
        return {}
    return _index_openapi(document, f"manifests/api/{spec_file}", findings)


def _load_yaml_or_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ModuleNotFoundError as error:
            raise ValueError("PyYAML is required to validate OpenAPI YAML") from error
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ValueError(f"invalid OpenAPI YAML: {error}") from error


def _index_openapi(
    document: Any, source: str, findings: _Findings
) -> dict[str, OperationParameters]:
    if not isinstance(document, Mapping):
        findings.error("openapi.document", source, "document must be an object")
        return {}
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        findings.error(
            "openapi.version", f"{source}#openapi", "must declare OpenAPI 3.x"
        )
    paths = document.get("paths", {})
    if not isinstance(paths, Mapping):
        findings.error("openapi.paths", f"{source}#paths", "must be an object")
        return {}
    operations: dict[str, OperationParameters] = {}
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path_name, path_item in paths.items():
        if not isinstance(path_name, str) or not isinstance(path_item, Mapping):
            continue
        inherited = path_item.get("parameters", [])
        placeholders = set(re.findall(r"\{([^}]+)\}", path_name))
        for method, operation in path_item.items():
            if str(method).lower() not in methods or not isinstance(operation, Mapping):
                continue
            operation_id = operation.get("operationId")
            if operation_id is None:
                continue
            location = f"{method.upper()} {path_name}"
            if not isinstance(operation_id, str) or not operation_id.strip():
                findings.error(
                    "openapi.operation_id", location, "operationId must be non-empty"
                )
                continue
            if operation_id in operations:
                findings.error(
                    "openapi.operation_id_duplicate",
                    location,
                    f"duplicate operationId {operation_id!r}",
                )
                continue
            parameters: list[Any] = []
            for candidate in (inherited, operation.get("parameters", [])):
                if isinstance(candidate, Sequence) and not isinstance(candidate, str):
                    parameters.extend(candidate)
            by_location: dict[str, set[str]] = {
                "path": set(placeholders),
                "query": set(),
                "header": set(),
                "cookie": set(),
            }
            for parameter in parameters:
                parameter = _resolve_parameter(document, parameter)
                if isinstance(parameter, Mapping):
                    where, name = parameter.get("in"), parameter.get("name")
                    if where in by_location and isinstance(name, str):
                        by_location[where].add(name)
            operations[operation_id] = OperationParameters(
                path=frozenset(by_location["path"]),
                query=frozenset(by_location["query"]),
                header=frozenset(by_location["header"]),
                cookie=frozenset(by_location["cookie"]),
            )
    findings.ok("openapi.loaded", source, f"parsed {len(operations)} operation(s)")
    return operations


def _resolve_parameter(document: Mapping[str, Any], parameter: Any) -> Any:
    if not isinstance(parameter, Mapping):
        return parameter
    reference = parameter.get("$ref")
    prefix = "#/components/parameters/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        return parameter
    components = document.get("components", {})
    parameters = (
        components.get("parameters", {}) if isinstance(components, Mapping) else {}
    )
    if isinstance(parameters, Mapping):
        return parameters.get(reference[len(prefix) :], parameter)
    return parameter


_PATH_TEMPLATE = re.compile(r"\{\{\s*pathParams\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")
_QUERY_TEMPLATE = re.compile(r"\{\{\s*queryParams\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def _validate_action_mappings(
    package: ManifestPackage,
    operations: Mapping[str, OperationParameters],
    findings: _Findings,
) -> None:
    checked = 0
    for resource_type in package.resource_type_definitions:
        for version in resource_type.versions:
            for action in version.actions:
                for mapping in action.all_open_api_mappings:
                    checked += 1
                    path = (
                        "manifests/api/resources.pkl#"
                        f"{resource_type.name}.{version.version}.{action.name}"
                    )
                    params = operations.get(mapping.operation_id)
                    if params is None:
                        findings.error(
                            "openapi.mapping_missing",
                            path,
                            f"operationId {mapping.operation_id!r} was not found",
                        )
                        continue
                    extractors = list(mapping.resource_name or ())
                    extractors.extend(
                        x
                        for x in (mapping.resource_id, mapping.new_name)
                        if x is not None
                    )
                    for extractor in extractors:
                        if (
                            isinstance(extractor, PathParam)
                            and extractor.name not in params.path
                        ):
                            findings.error(
                                "openapi.path_parameter",
                                path,
                                f"PathParam {extractor.name!r} is not declared",
                            )
                        elif (
                            isinstance(extractor, QueryParam)
                            and extractor.name not in params.query
                        ):
                            findings.error(
                                "openapi.query_parameter",
                                path,
                                f"QueryParam {extractor.name!r} is not declared",
                            )
                        elif not isinstance(
                            extractor, (PathParam, QueryParam, BodyField, ResponseField)
                        ):
                            findings.error(
                                "openapi.extractor", path, "unknown resource extractor"
                            )
                    for template in filter(
                        None,
                        (
                            mapping.resource_name_template,
                            mapping.resource_id_template,
                            mapping.new_name_template,
                        ),
                    ):
                        for name in _PATH_TEMPLATE.findall(template):
                            if name not in params.path:
                                findings.error(
                                    "openapi.path_template",
                                    path,
                                    f"path parameter {name!r} is not declared",
                                )
                        for name in _QUERY_TEMPLATE.findall(template):
                            if name not in params.query:
                                findings.error(
                                    "openapi.query_template",
                                    path,
                                    f"query parameter {name!r} is not declared",
                                )
    if checked:
        findings.ok(
            "openapi.mappings_checked",
            "manifests/api/resources.pkl",
            f"checked {checked} mapping(s)",
        )


def _validate_type_specific(
    package: ManifestPackage, manifests: Path, findings: _Findings
) -> None:
    app_type = package.application.app_type
    deployment = manifests / "deployment"
    if app_type is ApplicationType.AGENT:
        if package.agent_spec is None:
            findings.error(
                "agent.spec_missing",
                "manifests/agent/spec.pkl",
                "Agent packages require agent/spec.pkl",
            )
        else:
            _validate_agent(package.agent_spec, findings)
        for name in (
            "components.pkl",
            "helm_charts.pkl",
            "helm_chart_values.pkl",
            "source",
        ):
            if (deployment / name).exists():
                findings.error(
                    "agent.deployment_forbidden",
                    f"manifests/deployment/{name}",
                    "Agent packages may only declare dependencies and migrations",
                )
    elif (manifests / "agent/spec.pkl").exists():
        findings.error(
            "agent.spec_unexpected",
            "manifests/agent/spec.pkl",
            f"{app_type.value} packages may not declare agent/spec.pkl",
        )

    if app_type is ApplicationType.CONNECTOR:
        if deployment.exists():
            for child in deployment.iterdir():
                findings.error(
                    "connector.deployment_forbidden",
                    f"manifests/deployment/{child.name}",
                    "Connector packages may not contain deployment inputs",
                )
        if package.api_spec is not None:
            _validate_connector(package, findings)

    if app_type is ApplicationType.APP:
        deployment_inputs = tuple(
            name
            for name in (
                "components.pkl",
                "dependencies.pkl",
                "helm_charts.pkl",
                "helm_chart_values.pkl",
            )
            if (deployment / name).is_file()
        )
        if deployment_inputs:
            findings.warn(
                "deployment.not_validated",
                "manifests/deployment/",
                "Python SDK does not yet schema-reflect deployment inputs; "
                f"run `tangram app manifest validate` for {', '.join(deployment_inputs)}",
            )

    integrations = manifests / "integrations"
    if integrations.is_dir() and any(integrations.glob("*.pkl")):
        findings.warn(
            "integrations.not_validated",
            "manifests/integrations/",
            "Python SDK does not yet decode or validate integration modules; "
            "run `tangram app manifest validate`",
        )


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PRIVILEGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:[A-Za-z*][A-Za-z0-9_*]*$")
_APP_COORDINATE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _validate_agent(spec: Mapping[str, Any], findings: _Findings) -> None:
    base = "manifests/agent/spec.pkl"
    prompt = spec.get("systemPrompt")
    if not isinstance(prompt, str) or not prompt.strip():
        findings.warn(
            "agent.system_prompt", f"{base}#systemPrompt", "systemPrompt is empty"
        )
    llm = spec.get("defaultLlm")
    if not isinstance(llm, Mapping) or not all(
        isinstance(llm.get(key), str) and llm[key].strip()
        for key in ("provider", "model")
    ):
        findings.error(
            "agent.default_llm",
            f"{base}#defaultLlm",
            "provider and model must be non-empty",
        )
    tools = spec.get("tools") or []
    if not isinstance(tools, Sequence) or isinstance(tools, str):
        findings.error("agent.tools", f"{base}#tools", "tools must be an array")
        tools = []
    names: set[str] = set()
    for index, tool in enumerate(tools):
        path = f"{base}#tools[{index}]"
        if not isinstance(tool, Mapping):
            findings.error("agent.tool", path, "tool must be an object")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            findings.error("agent.tool_name", path, "name must match ^[a-z][a-z0-9_]*$")
        elif name in names:
            findings.error("agent.tool_duplicate", path, f"duplicate tool {name!r}")
        else:
            names.add(name)
        description = tool.get("description")
        if not isinstance(description, str) or not description.strip():
            findings.error(
                "agent.tool_description", path, "description must be non-empty"
            )
        privilege = tool.get("requiredPrivilege")
        if privilege is not None and (
            not isinstance(privilege, str) or not _PRIVILEGE.fullmatch(privilege)
        ):
            findings.error(
                "agent.tool_privilege",
                path,
                "requiredPrivilege must be ResourceType:Privilege",
            )
        if tool.get("requiresConfirmation") is None:
            findings.warn(
                "agent.tool_confirmation",
                path,
                "requiresConfirmation is omitted and defaults to true",
            )
        _validate_agent_binding(tool.get("defaultBinding"), path, findings)
    _validate_skills(spec.get("skills") or [], base, findings)


def _validate_skills(skills: Any, base: str, findings: _Findings) -> None:
    if not isinstance(skills, Sequence) or isinstance(skills, str):
        findings.error("agent.skills", f"{base}#skills", "skills must be an array")
        return
    seen: set[tuple[str, str]] = set()
    for index, skill in enumerate(skills):
        path = f"{base}#skills[{index}]"
        if not isinstance(skill, Mapping):
            findings.error("agent.skill", path, "skill must be an object")
            continue
        name, version = skill.get("name"), skill.get("version")
        if not isinstance(name, str) or not name.strip():
            findings.error("agent.skill_name", path, "name must be non-empty")
        if not isinstance(version, str) or not version.strip():
            findings.error("agent.skill_version", path, "version must be non-empty")
        if isinstance(name, str) and isinstance(version, str):
            key = (name, version)
            if key in seen:
                findings.error(
                    "agent.skill_duplicate", path, f"duplicate skill {name}@{version}"
                )
            seen.add(key)


def _validate_agent_binding(binding: Any, path: str, findings: _Findings) -> None:
    if binding is None:
        return
    if not isinstance(binding, Mapping):
        findings.error("agent.binding", path, "defaultBinding must be an object")
        return
    kind = binding.get("kind")
    if kind == "AppAction":
        app, action = binding.get("app"), binding.get("action")
        if not isinstance(app, str) or not _APP_COORDINATE.fullmatch(app):
            findings.error("agent.binding_app", path, "binding app must be group/name")
        if not isinstance(action, str) or not action.strip():
            findings.error(
                "agent.binding_action", path, "binding action must be non-empty"
            )
    elif kind == "HttpEndpoint":
        method, url = binding.get("method"), binding.get("url")
        if not isinstance(method, str) or method.upper() not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            findings.error("agent.binding_method", path, "unsupported HTTP method")
        if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
            findings.error(
                "agent.binding_url", path, "URL must start with http:// or https://"
            )
    elif kind == "Builtin":
        name = binding.get("name")
        if not isinstance(name, str) or not name.strip():
            findings.error(
                "agent.binding_builtin", path, "builtin name must be non-empty"
            )
    else:
        findings.error("agent.binding_kind", path, f"unknown binding kind {kind!r}")


def _validate_connector(package: ManifestPackage, findings: _Findings) -> None:
    assert package.api_spec is not None
    spec = package.api_spec
    base = "manifests/api/spec.pkl"
    endpoint = spec.get("endpoint")
    allowlist = spec.get("endpointHostAllowlist") or []
    parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
    if endpoint is not None and (
        parsed is None or parsed.scheme != "https" or not parsed.hostname
    ):
        findings.error("connector.endpoint", f"{base}#endpoint", "must be an HTTPS URL")
    pattern = re.compile(r"^(?:\*\.)?[a-z0-9][a-z0-9.\-]*$")
    for index, item in enumerate(allowlist):
        if not isinstance(item, str) or not pattern.fullmatch(item.lower()):
            findings.error(
                "connector.allowlist_pattern",
                f"{base}#endpointHostAllowlist[{index}]",
                "invalid host pattern",
            )
    if (
        parsed is not None
        and parsed.hostname
        and allowlist
        and not any(_host_matches(parsed.hostname, item) for item in allowlist)
    ):
        findings.error(
            "connector.endpoint_allowlist",
            f"{base}#endpoint",
            "default endpoint does not match endpointHostAllowlist",
        )
    if (
        spec.get("endpointRequired") is True
        and endpoint is None
        and not spec.get("endpointOverridable", False)
    ):
        findings.error(
            "connector.endpoint_required",
            base,
            "endpointRequired without a default requires endpointOverridable",
        )
    oauth = spec.get("oauth")
    if isinstance(oauth, Mapping):
        _validate_oauth(package, oauth, base, findings)


def _validate_oauth(
    package: ManifestPackage,
    oauth: Mapping[str, Any],
    base: str,
    findings: _Findings,
) -> None:
    for field in ("authorizationUrl", "tokenUrl", "revocationUrl"):
        value = oauth.get(field)
        if value is not None:
            parsed = urlparse(value) if isinstance(value, str) else None
            if parsed is None or parsed.scheme != "https" or not parsed.hostname:
                findings.error(
                    "connector.oauth_url",
                    f"{base}#oauth.{field}",
                    "must be an HTTPS URL",
                )
    if oauth.get("clientIdSecret") == oauth.get("clientSecretSecret"):
        findings.error(
            "connector.oauth_secrets",
            f"{base}#oauth",
            "clientIdSecret and clientSecretSecret must differ",
        )
    expected = (
        "/api/core/v1/connector-oauth/callback/"
        f"{package.application.group}/{package.application.name}"
    )
    if oauth.get("callbackPath") != expected:
        findings.error(
            "connector.oauth_callback",
            f"{base}#oauth.callbackPath",
            f"must equal {expected!r}",
        )
    window = oauth.get("refreshWindowSeconds", 600)
    if not isinstance(window, int) or not 0 < window < 3600:
        findings.error(
            "connector.oauth_refresh",
            f"{base}#oauth.refreshWindowSeconds",
            "must be between 0 and 3600 seconds",
        )


def _host_matches(host: str, pattern: Any) -> bool:
    if not isinstance(pattern, str):
        return False
    host, pattern = host.lower(), pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == pattern


def _validate_ui(
    package: ManifestPackage, manifests: Path, findings: _Findings
) -> None:
    spec = package.ui_spec
    if spec is None:
        return
    base = "manifests/ui/spec.pkl"
    components = spec.get("components") or []
    if not isinstance(components, Sequence) or isinstance(components, str):
        findings.error("ui.components", f"{base}#components", "must be an array")
        return
    names: set[str] = set()
    for index, component in enumerate(components):
        path = f"{base}#components[{index}]"
        if not isinstance(component, Mapping):
            findings.error("ui.component", path, "must be an object")
            continue
        name, kind = component.get("name"), component.get("kind")
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name
        ):
            findings.error("ui.component_name", path, "invalid component name")
            continue
        if name in names:
            findings.error(
                "ui.component_duplicate", path, f"duplicate component {name!r}"
            )
        names.add(name)
        if kind not in {"declarative", "sandboxed"}:
            findings.error(
                "ui.component_kind", path, f"unknown component kind {kind!r}"
            )
        if kind == "declarative" and component.get("spec") is None:
            findings.error(
                "ui.component_spec", path, "declarative component requires spec"
            )
        if kind == "sandboxed" and component.get("entry") is None:
            findings.error(
                "ui.component_entry", path, "sandboxed component requires entry"
            )
        for field in ("entry", "spec"):
            value = component.get(field)
            if isinstance(value, str):
                _validate_artifact_path(
                    manifests / "ui", value, f"{path}.{field}", findings
                )
        entry = component.get("entry")
        if kind == "sandboxed" and isinstance(entry, str):
            parts = entry.split("/")
            if (
                len(parts) != 3
                or parts[0] != "components"
                or parts[1] != name
                or not parts[2]
            ):
                findings.error(
                    "ui.component_entry_shape",
                    f"{path}.entry",
                    f"must have the form components/{name}/<entry-file>",
                )
    deployment = spec.get("deployment")
    if isinstance(deployment, Mapping) and deployment.get("mode") == "UIComponent":
        root_component = deployment.get("rootComponent")
        if root_component is not None and root_component not in names:
            findings.error(
                "ui.root_component",
                f"{base}#deployment.rootComponent",
                f"component {root_component!r} is not declared",
            )
    logo = spec.get("logo")
    if isinstance(logo, str):
        if not logo.startswith("static/"):
            findings.error("ui.static_path", f"{base}#logo", "must start with static/")
        _validate_artifact_path(manifests / "ui", logo, f"{base}#logo", findings)
    icons = spec.get("resourceTypeIcons") or {}
    if isinstance(icons, Mapping):
        for resource_type, value in icons.items():
            path = f"{base}#resourceTypeIcons.{resource_type}"
            if isinstance(value, str):
                if not value.startswith("static/"):
                    findings.error("ui.static_path", path, "must start with static/")
                _validate_artifact_path(manifests / "ui", value, path, findings)


def _validate_artifact_path(
    base: Path, value: str, path: str, findings: _Findings
) -> None:
    relative = Path(value)
    target = (base / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not target.is_relative_to(base.resolve())
    ):
        findings.error("path.traversal", path, "artifact path escapes manifests/ui/")
    elif not target.is_file():
        findings.error("path.missing", path, f"artifact {value!r} does not exist")
