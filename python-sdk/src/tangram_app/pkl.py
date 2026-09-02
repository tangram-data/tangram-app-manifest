"""Restricted Pkl subprocess integration and manifest package loading."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Protocol

from .errors import ManifestDecodeError, PklEvaluationError, PklNotFoundError
from .manifest import (
    AppResourceTypeDefinition,
    Application,
    ConfigField,
    ManifestPackage,
    _array,
    _freeze,
    _object,
)


class Evaluator(Protocol):
    def evaluate(
        self,
        module: Path,
        *,
        expression: str | None,
        root_dir: Path,
        project_dir: Path | None,
    ) -> Any: ...


class PklEvaluator:
    """Drive the official Pkl CLI and return its JSON projection.

    File access is rooted at the package root. Ambient environment,
    external-property resources, and direct ``package:`` imports are not
    allowed. Dependency aliases declared by PklProject remain available via
    ``projectpackage:`` URIs.
    """

    _ALLOWED_MODULES = "pkl:.*,file:.*,projectpackage:.*,repl:.*"
    _ALLOWED_RESOURCES = "file:.*,projectpackage:.*,prop:pkl\\..*"

    def __init__(
        self, executable: str | Path = "pkl", *, timeout_seconds: float = 30.0
    ) -> None:
        resolved = shutil.which(str(executable))
        if resolved is None and str(executable) == "pkl":
            # Fall back to the doctor-managed copy (no PATH edits needed).
            from .local_doctor import find_pkl

            resolved = find_pkl()
        if resolved is None:
            raise PklNotFoundError(
                f"Pkl executable {str(executable)!r} was not found; run "
                "`tangram-app doctor --fix` to install it, or get it from pkl-lang.org"
            )
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def evaluate(
        self,
        module: Path,
        *,
        expression: str | None,
        root_dir: Path,
        project_dir: Path | None,
    ) -> Any:
        module = module.resolve()
        root_dir = root_dir.resolve()
        if not module.is_relative_to(root_dir):
            raise PklEvaluationError(
                f"module {module} is outside package root {root_dir}"
            )
        command = [
            self.executable,
            "eval",
            "--format",
            "json",
            "--root-dir",
            str(root_dir),
            "--allowed-modules",
            self._ALLOWED_MODULES,
            "--allowed-resources",
            self._ALLOWED_RESOURCES,
            "--omit-project-settings",
            "--timeout",
            f"{self.timeout_seconds:g}",
        ]
        if project_dir is not None:
            command.extend(("--project-dir", str(project_dir.resolve())))
        if expression is not None:
            rendered = (
                "new JsonRenderer { omitNullProperties = false }"
                f".renderValue({expression})"
            )
            command.extend(("--expression", rendered))
        command.append(str(module))
        try:
            completed = subprocess.run(
                command,
                cwd=str(module.parent),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 1,
            )
        except subprocess.TimeoutExpired as error:
            raise PklEvaluationError(
                f"Pkl evaluation timed out for {module.relative_to(root_dir)}"
            ) from error
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "unknown Pkl error"
            )
            raise PklEvaluationError(
                f"Pkl evaluation failed for {module.relative_to(root_dir)}: {detail}"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PklEvaluationError(
                f"Pkl returned invalid JSON for {module.relative_to(root_dir)}: {error}"
            ) from error


class PklManifestLoader:
    """Evaluate the standard package entry points into Python dataclasses."""

    def __init__(self, evaluator: Evaluator | None = None) -> None:
        self.evaluator = evaluator or PklEvaluator()

    def load(self, package_root: str | Path) -> ManifestPackage:
        root = Path(package_root).resolve()
        manifests = root / "manifests"
        if not manifests.is_dir():
            raise ManifestDecodeError(f"{root} does not contain manifests/")
        app_file = manifests / "app.pkl"
        if not app_file.is_file():
            raise ManifestDecodeError(f"{app_file} does not exist")
        project_dir = manifests if (manifests / "PklProject").is_file() else None

        application = Application.from_dict(
            self._evaluate(app_file, None, root, project_dir)
        )
        resource_types = self._load_list(
            manifests / "api/resources.pkl",
            "types",
            root,
            project_dir,
            AppResourceTypeDefinition.from_dict,
        )
        settings = self._load_list(
            manifests / "settings.pkl",
            "settings",
            root,
            project_dir,
            ConfigField.from_dict,
        )
        secrets = self._load_list(
            manifests / "secrets.pkl",
            "secrets",
            root,
            project_dir,
            ConfigField.from_dict,
        )
        api_spec = self._load_optional_object(
            manifests / "api/spec.pkl", root, project_dir
        )
        agent_spec = self._load_optional_object(
            manifests / "agent/spec.pkl", root, project_dir
        )
        ui_spec = self._load_optional_object(
            manifests / "ui/spec.pkl", root, project_dir
        )
        return ManifestPackage(
            application=application,
            resource_type_definitions=resource_types,
            settings=settings,
            secrets=secrets,
            api_spec=api_spec,
            agent_spec=agent_spec,
            ui_spec=ui_spec,
            source_root=root,
        )

    def _evaluate(
        self,
        module: Path,
        expression: str | None,
        root: Path,
        project_dir: Path | None,
    ) -> Any:
        return self.evaluator.evaluate(
            module,
            expression=expression,
            root_dir=root,
            project_dir=project_dir,
        )

    def _load_list(self, module, expression, root, project_dir, decoder):
        if not module.is_file():
            return ()
        raw = self._evaluate(module, expression, root, project_dir)
        return tuple(
            decoder(item, f"{module.relative_to(root)}[{index}]")
            for index, item in enumerate(_array(raw, str(module.relative_to(root))))
        )

    def _load_optional_object(self, module: Path, root: Path, project_dir: Path | None):
        if not module.is_file():
            return None
        raw = self._evaluate(module, None, root, project_dir)
        return _freeze(_object(raw, str(module.relative_to(root))))
