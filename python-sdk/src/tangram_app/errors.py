"""Public SDK exceptions."""

from __future__ import annotations


class TangramAppError(Exception):
    """Base exception for the standalone Tangram App SDK."""


class CapabilityGraphError(TangramAppError, ValueError):
    """The capability graph is malformed or unsupported."""


class CapabilityGraphStaleError(CapabilityGraphError):
    """A generated artifact no longer matches its integrity lock."""


class UnsupportedRequirementError(TangramAppError, RuntimeError):
    """The standalone host cannot satisfy a declared runtime requirement."""


class LocalRuntimeError(TangramAppError, RuntimeError):
    """The canonical local app source runtime could not start or stop safely."""


class BackendContractError(LocalRuntimeError, ValueError):
    """The running backend does not implement the compiled app contract."""


class UnknownBindingError(TangramAppError, LookupError):
    """No executable action binding has the requested id."""


class AmbiguousActionError(TangramAppError, LookupError):
    """An action id names more than one executable binding."""


class InputValidationError(TangramAppError, ValueError):
    """Invocation arguments do not satisfy the binding input schema."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


class OutputValidationError(TangramAppError, ValueError):
    """A driver result does not satisfy the binding output schema."""

    def __init__(self, binding_id: str, validation: InputValidationError):
        self.binding_id = binding_id
        self.path = validation.path
        self.message = validation.message
        super().__init__(f"{binding_id} output {validation}")


class PolicyDeniedError(TangramAppError, PermissionError):
    """Host policy denied an invocation."""

    def __init__(self, binding_id: str, reason: str):
        self.binding_id = binding_id
        self.reason = reason
        super().__init__(f"{binding_id}: {reason}")


class ConfirmationRequiredError(PolicyDeniedError):
    """The invocation requires confirmation not available to this call."""


class DriverError(TangramAppError, RuntimeError):
    """The selected execution driver could not invoke the binding."""


class RequestRenderError(DriverError, ValueError):
    """Arguments cannot be rendered into the selected transport request."""


class HttpResponseError(DriverError):
    """An HTTP execution driver received a non-success response."""

    def __init__(self, status: int, reason: str, *, retryable: bool):
        self.status = status
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"HTTP {status}: {reason}")


class ManifestDecodeError(TangramAppError, ValueError):
    """Evaluated manifest JSON does not match the public Tangram model."""


class PklEvaluationError(TangramAppError, RuntimeError):
    """Pkl could not evaluate a manifest entry point."""


class PklNotFoundError(PklEvaluationError):
    """The configured Pkl executable could not be found."""


class ManifestValidationError(TangramAppError, ValueError):
    """A manifest validation result contains one or more errors."""


class ManifestCompilationError(TangramAppError, ValueError):
    """A validated manifest cannot be compiled into a capability graph."""
