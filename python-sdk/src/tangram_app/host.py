"""Transport-independent Tangram action host."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .errors import (
    ConfirmationRequiredError,
    DriverError,
    InputValidationError,
    OutputValidationError,
    PolicyDeniedError,
    RequestRenderError,
)
from .models import Action, ActionBinding, CapabilityGraph
from .policy import (
    AuthorizationPolicy,
    DecisionKind,
    InvocationRequest,
    LocalDevelopmentPolicy,
    Principal,
)
from .schema import validate


class ExecutionDriver(Protocol):
    async def invoke(
        self, action: Action, binding: ActionBinding, arguments: Any
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    package_digest: str
    principal_id: str
    principal_kind: str
    action_id: str
    binding_id: str
    effect: str
    decision: str
    outcome: str
    arguments_hash: str
    error_type: str | None = None


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    async def record(self, event: AuditEvent) -> None:
        return None


class MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAuditSink:
    """Diagnostic local audit. The file is not a tamper-proof audit store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def record(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
            )
            stream.write("\n")


Handler = Callable[[Any], Awaitable[Any]]


class InMemoryDriver:
    """Execution driver for tests and embedded Python applications."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def handler(self, binding_id: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            if binding_id in self._handlers:
                raise ValueError(f"handler already registered for {binding_id!r}")
            self._handlers[binding_id] = handler
            return handler

        return register

    async def invoke(
        self, action: Action, binding: ActionBinding, arguments: Any
    ) -> Any:
        handler = self._handlers.get(binding.id)
        if handler is None:
            raise DriverError(f"no handler registered for {binding.id!r}")
        return await handler(arguments)


class TangramHost:
    def __init__(
        self,
        graph: CapabilityGraph,
        *,
        driver: ExecutionDriver,
        policy: AuthorizationPolicy | None = None,
        audit: AuditSink | None = None,
        principal: Principal | None = None,
    ) -> None:
        self.graph = graph
        self.driver = driver
        self.policy = policy or LocalDevelopmentPolicy()
        self.audit = audit or NullAuditSink()
        self.principal = principal or Principal("local-user")

    async def call(self, id: str, arguments: Any) -> Any:
        action, binding = self.graph.resolve(id)
        request = InvocationRequest(self.principal, action, binding, arguments)
        arguments_hash = _json_hash(arguments)
        try:
            validate(arguments, binding.input_schema)
        except InputValidationError as error:
            await self._audit(
                request,
                decision="not_evaluated",
                outcome="invalid_input",
                arguments_hash=arguments_hash,
                error_type=f"{type(error).__module__}.{type(error).__qualname__}",
            )
            raise
        decision = await self.policy.authorize(request)
        if decision.kind is not DecisionKind.ALLOW:
            await self._audit(
                request,
                decision=decision.kind.value,
                outcome="denied",
                arguments_hash=arguments_hash,
            )
            if decision.kind is DecisionKind.CONFIRMATION_REQUIRED:
                raise ConfirmationRequiredError(binding.id, decision.reason)
            raise PolicyDeniedError(binding.id, decision.reason)

        try:
            result = await self.driver.invoke(action, binding, arguments)
            if binding.output_schema is not None:
                try:
                    validate(result, binding.output_schema)
                except InputValidationError as error:
                    raise OutputValidationError(binding.id, error) from error
        except RequestRenderError as error:
            await self._audit(
                request,
                decision=decision.kind.value,
                outcome="invalid_input",
                arguments_hash=arguments_hash,
                error_type=f"{type(error).__module__}.{type(error).__qualname__}",
            )
            raise
        except Exception as error:
            await self._audit(
                request,
                decision=decision.kind.value,
                outcome="error",
                arguments_hash=arguments_hash,
                error_type=f"{type(error).__module__}.{type(error).__qualname__}",
            )
            raise
        await self._audit(
            request,
            decision=decision.kind.value,
            outcome="success",
            arguments_hash=arguments_hash,
        )
        return result

    async def _audit(
        self,
        request: InvocationRequest,
        *,
        decision: str,
        outcome: str,
        arguments_hash: str,
        error_type: str | None = None,
    ) -> None:
        await self.audit.record(
            AuditEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                package_digest=self.graph.package.digest,
                principal_id=request.principal.id,
                principal_kind=request.principal.kind,
                action_id=request.action.id,
                binding_id=request.binding.id,
                effect=request.action.effect.value,
                decision=decision,
                outcome=outcome,
                arguments_hash=arguments_hash,
                error_type=error_type,
            )
        )


def _json_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RequestRenderError(
            f"arguments are not JSON serializable: {error}"
        ) from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
