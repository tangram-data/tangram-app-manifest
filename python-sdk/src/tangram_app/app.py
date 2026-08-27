"""Developer-facing facade over compilation, discovery, binding, and invocation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .compiler import CompilationResult, compile_manifest
from .errors import UnsupportedRequirementError
from .host import AuditSink, JsonlAuditSink, TangramHost
from .http_driver import LocalHttpDriver
from .models import CapabilityGraph
from .policy import AuthorizationPolicy, LocalDevelopmentPolicy
from .validation import ValidationFinding


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    action_id: str
    resource_type: str
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    effect: str
    idempotent: bool
    requires_confirmation: bool
    required_privileges: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "actionId": self.action_id,
            "resourceType": self.resource_type,
            "name": self.name,
            "description": self.description,
            "inputSchema": _plain(self.input_schema),
            "effect": self.effect,
            "idempotent": self.idempotent,
            "requiresConfirmation": self.requires_confirmation,
            "requiredPrivileges": list(self.required_privileges),
        }
        if self.output_schema is not None:
            value["outputSchema"] = _plain(self.output_schema)
        return value


@dataclass(frozen=True, slots=True)
class TangramApp:
    """A compiled app, optionally bound to a standalone execution host."""

    graph: CapabilityGraph
    findings: tuple[ValidationFinding, ...] = ()
    authority: str = "unknown"
    source_root: Path | None = None
    _host: TangramHost | None = None
    _audit_mode: str | None = None
    _validation_mode: str = "snapshot"

    @classmethod
    def from_package(cls, package_root: str | Path) -> "TangramApp":
        result: CompilationResult = compile_manifest(package_root)
        return cls(
            graph=result.graph,
            findings=result.findings,
            authority=result.graph.authority,
            source_root=Path(package_root).resolve(),
            _validation_mode="source",
        )

    @classmethod
    def from_graph(cls, graph: CapabilityGraph | str | Path) -> "TangramApp":
        loaded = (
            graph
            if isinstance(graph, CapabilityGraph)
            else CapabilityGraph.from_file(graph)
        )
        return cls(graph=loaded, authority=loaded.authority)

    def bind(
        self,
        *,
        backend: str,
        policy: AuthorizationPolicy | str | None = None,
        audit: AuditSink | None = None,
        audit_path: str | Path | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> "TangramApp":
        unsupported = _unsupported_runtime_requirements(self.graph)
        if unsupported:
            raise UnsupportedRequirementError(
                "standalone host cannot satisfy: " + ", ".join(unsupported)
            )
        if audit is not None and audit_path is not None:
            raise ValueError("configure audit or audit_path, not both")
        resolved_policy: AuthorizationPolicy
        if policy is None or policy == "local-development":
            resolved_policy = LocalDevelopmentPolicy()
        elif isinstance(policy, str):
            raise ValueError(f"unsupported policy {policy!r}")
        else:
            resolved_policy = policy
        resolved_audit = audit or (
            JsonlAuditSink(audit_path) if audit_path is not None else None
        )
        host = TangramHost(
            self.graph,
            driver=LocalHttpDriver(
                backend,
                headers=headers,
                timeout_seconds=timeout_seconds,
            ),
            policy=resolved_policy,
            audit=resolved_audit,
        )
        audit_mode = (
            "custom-provider"
            if audit is not None
            else "local-jsonl"
            if audit_path is not None
            else "disabled"
        )
        return replace(self, _host=host, _audit_mode=audit_mode)

    def tools(self) -> tuple[ToolDefinition, ...]:
        tools: list[ToolDefinition] = []
        for action in self.graph.actions:
            for binding in action.bindings:
                tools.append(
                    ToolDefinition(
                        id=binding.id,
                        action_id=action.id,
                        resource_type=action.resource_type,
                        name=action.name,
                        description=action.description,
                        input_schema=binding.input_schema,
                        output_schema=binding.output_schema,
                        effect=action.effect.value,
                        idempotent=action.idempotent,
                        requires_confirmation=action.requires_confirmation,
                        required_privileges=action.required_privileges,
                    )
                )
        return tuple(tools)

    def ui(self) -> dict[str, Any] | None:
        return _plain(self.graph.ui) if self.graph.ui else None

    def capabilities(self) -> dict[str, Any]:
        requirements = self.graph.runtime_requirements
        backend = requirements.get("backend")
        settings = requirements.get("settings", [])
        secrets = requirements.get("secrets", [])
        infrastructure = requirements.get("infrastructureClaims", [])
        unsupported_runtime = _unsupported_runtime_requirements(self.graph)
        blocked_actions = []
        for action in self.graph.actions:
            if action.effect.value != "Stateless":
                blocked_actions.append(
                    {
                        "action": action.id,
                        "reason": "mutations are disabled by local policy",
                    }
                )
            elif action.requires_confirmation:
                blocked_actions.append(
                    {
                        "action": action.id,
                        "reason": "per-call confirmation is unavailable",
                    }
                )
        if unsupported_runtime:
            reason = "unresolved runtime requirements: " + ", ".join(
                unsupported_runtime
            )
            blocked = {item["action"] for item in blocked_actions}
            for action in self.graph.actions:
                if action.id not in blocked:
                    blocked_actions.append({"action": action.id, "reason": reason})
        audit_capability: dict[str, str]
        if self._audit_mode == "local-jsonl":
            audit_capability = {"state": "emulated", "detail": "local-jsonl"}
        elif self._audit_mode == "custom-provider":
            audit_capability = {"state": "delegated", "detail": "custom-provider"}
        elif self._audit_mode == "disabled":
            audit_capability = {"state": "unsupported", "detail": "disabled"}
        else:
            audit_capability = {"state": "delegated", "detail": "configure-on-bind"}
        validation = (
            {"state": "enforced"}
            if self._validation_mode == "source"
            else {"state": "delegated", "detail": "compiled-artifact"}
        )
        return {
            "status": "blocked" if unsupported_runtime else "ready_with_limits",
            "authority": self.authority,
            "developmentOnly": self.graph.development_only,
            "ui": self.ui(),
            "capabilities": {
                "manifestValidation": validation,
                "openApiValidation": validation,
                "authorization": {"state": "emulated", "detail": "local-policy"},
                "perCallConfirmation": {"state": "unsupported"},
                "audit": audit_capability,
                "settings": _requirement_capability(settings, backend),
                "secrets": _requirement_capability(secrets, backend),
                "oauth": {"state": "unsupported"},
                "infrastructureClaims": _requirement_capability(
                    infrastructure, backend
                ),
                "uiSandbox": {"state": "unsupported"},
            },
            "blockedActions": blocked_actions,
        }

    async def call(self, id: str, arguments: Any) -> Any:
        if self._host is None:
            raise RuntimeError(
                "TangramApp must be bound to a backend before calling actions"
            )
        return await self._host.call(id, arguments)

    def run_local(
        self,
        *,
        python: str | Path | None = None,
        startup_timeout_seconds: float = 30.0,
        request_timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
        audit_path: str | Path | None = None,
        managed_environment: bool = True,
        policy: AuthorizationPolicy | str | None = None,
    ):
        """Start this source package's canonical local Python backend."""
        from .local_runtime import LocalSourceRuntime

        return LocalSourceRuntime(
            python=python,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            environment=environment,
            audit_path=audit_path,
            managed_environment=managed_environment,
            policy=policy,
        ).start(self)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _requirement_capability(items: Any, backend: Any) -> dict[str, str]:
    if not items:
        return {"state": "not-required"}
    if backend == "service":
        return {"state": "delegated", "detail": "app-backend"}
    return {"state": "unsupported", "detail": "unresolved"}


def _unsupported_runtime_requirements(graph: CapabilityGraph) -> tuple[str, ...]:
    requirements = graph.runtime_requirements
    backend = requirements.get("backend")
    unsupported: list[str] = []
    if backend == "agent":
        unsupported.append("agent runtime")
    if backend != "service":
        for category in ("settings", "secrets", "infrastructureClaims"):
            raw_items = requirements.get(category, ())
            if not isinstance(raw_items, (list, tuple)):
                continue
            names = [
                str(item.get("name", category))
                for item in raw_items
                if isinstance(item, Mapping) and item.get("required", True)
            ]
            if names:
                unsupported.append(f"{category} ({', '.join(names)})")
    return tuple(unsupported)
