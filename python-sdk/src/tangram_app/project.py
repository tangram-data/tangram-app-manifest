"""Mutable source-project facade for AI-assisted Tangram app authoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .app import TangramApp
from .local_runtime import BackendSpec, LocalAppSession, LocalSourceRuntime
from .policy import AuthorizationPolicy
from .validation import ValidationResult, validate_manifest


@dataclass(frozen=True, slots=True)
class TangramProject:
    """A canonical Tangram source package being authored or run locally."""

    root: Path

    @classmethod
    def open(cls, package_root: str | Path) -> "TangramProject":
        root = Path(package_root).resolve()
        if not (root / "manifests").is_dir():
            raise ValueError(f"{root} does not contain manifests/")
        return cls(root)

    def validate(self) -> ValidationResult:
        return validate_manifest(self.root)

    def compile(self) -> TangramApp:
        return TangramApp.from_package(self.root)

    def backend_spec(self) -> BackendSpec:
        return BackendSpec.from_project(self.root)

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
    ) -> LocalAppSession:
        runtime = LocalSourceRuntime(
            python=python,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            environment=environment,
            audit_path=audit_path,
            managed_environment=managed_environment,
            policy=policy,
        )
        return runtime.start(self.compile())
