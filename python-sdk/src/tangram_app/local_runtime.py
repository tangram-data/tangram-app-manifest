"""Host-native runtime for canonical agent-built Python app backends."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from types import TracebackType
from typing import TYPE_CHECKING, Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .errors import (
    BackendContractError,
    LocalRuntimeError,
    UnsupportedRequirementError,
)
from .local_database import LocalPostgres, apply_migrations, load_migrations, start_postgres
from .local_ui import LocalUiServer

if TYPE_CHECKING:
    from .app import TangramApp, ToolDefinition
    from .policy import AuthorizationPolicy


_SOURCE_DIR = Path("manifests/deployment/source/backend")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_MAX_OPENAPI_BYTES = 4 * 1024 * 1024
_BASE_PINS = (
    "requests==2.34.2",
    "httpx==0.28.1",
    "pydantic==2.13.4",
    "psycopg[binary,pool]==3.3.4",
    "fastapi==0.141.1",
    "starlette==1.3.1",
    "uvicorn==0.52.0",
    "opentelemetry-api==1.44.0",
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Canonical backend declaration read from the app's pyproject.toml."""

    root: Path
    source_dir: Path
    entry: str
    runtime: str
    dependencies: tuple[str, ...]
    egress: tuple[str, ...]

    @classmethod
    def from_project(cls, package_root: str | Path) -> "BackendSpec":
        root = Path(package_root).resolve()
        backend = root / _SOURCE_DIR
        pyproject = backend / "pyproject.toml"
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise LocalRuntimeError(
                f"this project has no Python backend ({pyproject} is missing)"
            ) from error
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise LocalRuntimeError(
                f"could not read backend pyproject.toml: {error}"
            ) from error

        tool = document.get("tool")
        tangram = tool.get("tangram") if isinstance(tool, Mapping) else None
        declaration = tangram.get("backend") if isinstance(tangram, Mapping) else None
        if not isinstance(declaration, Mapping):
            raise LocalRuntimeError(
                "pyproject.toml has no [tool.tangram.backend] table"
            )
        entry_value = declaration.get("entry")
        if not isinstance(entry_value, str) or not entry_value.strip():
            raise LocalRuntimeError(
                "[tool.tangram.backend].entry must be a module name"
            )
        entry = _entry_module(entry_value.strip())
        runtime = declaration.get("runtime", "python-3.12")
        if runtime != "python-3.12":
            raise LocalRuntimeError(
                f"unsupported local backend runtime {runtime!r}; expected 'python-3.12'"
            )
        egress = _string_array(declaration.get("egress", []), "egress")
        project = document.get("project")
        dependencies = _string_array(
            project.get("dependencies", []) if isinstance(project, Mapping) else [],
            "project.dependencies",
        )
        source_dir = (backend / "src").resolve()
        if not source_dir.is_dir():
            raise LocalRuntimeError(f"backend source directory {source_dir} is missing")
        entry_file = source_dir.joinpath(*entry.split(".")).with_suffix(".py")
        if not entry_file.is_file() or not entry_file.resolve().is_relative_to(
            source_dir
        ):
            raise LocalRuntimeError(
                f"backend entry module {entry!r} does not resolve to a source file"
            )
        return cls(root, source_dir, entry, runtime, dependencies, egress)


class LocalAppSession:
    """A running source backend bound to the normal governed Tangram host."""

    def __init__(
        self,
        app: "TangramApp",
        *,
        backend_url: str,
        process: subprocess.Popen[bytes],
        log_path: Path,
        log_file,
        database: LocalPostgres | None,
        project_lock: "_ProjectLock",
        request_timeout_seconds: float,
        actions_server=None,
        audit_path=None,
    ) -> None:
        self.app = app
        self.backend_url = backend_url
        self.process = process
        self.log_path = log_path
        self._log_file = log_file
        self.database = database
        self._project_lock = project_lock
        self.request_timeout_seconds = request_timeout_seconds
        self._ui_server: LocalUiServer | None = None
        self._actions_server = actions_server
        self.audit_path = audit_path
        self._closed = False

    @property
    def ui_url(self) -> str | None:
        return self._ui_server.url if self._ui_server is not None else None

    def _attach_ui(self, server: LocalUiServer | None) -> None:
        self._ui_server = server

    def tools(self) -> tuple["ToolDefinition", ...]:
        return self.app.tools()

    def capabilities(self) -> dict[str, Any]:
        report = self.app.capabilities()
        if self.database is not None:
            report["capabilities"]["infrastructureClaims"] = {
                "state": "enforced",
                "detail": "local-postgresql",
            }
        if self._ui_server is not None:
            report["capabilities"]["uiSandbox"] = {
                "state": "emulated",
                "detail": "loopback-component-host",
            }
        report["capabilities"]["ownActions"] = {
            "state": "emulated",
            "detail": "loopback-host-pipeline, unattended (confirmation-gated actions refuse)",
        }
        report["capabilities"]["crossAppActions"] = {
            "state": "unsupported",
            "detail": "requires Tangram OS (declared backend app dependencies)",
        }
        report["capabilities"]["workspaceSql"] = {
            "state": "unsupported",
            "detail": "requires Tangram OS (declared workspace queries)",
        }
        report["capabilities"]["schedules"] = {
            "state": "emulated",
            "detail": (
                "host-side scheduler firing own unattended actions; fires only while "
                "the session runs; Unix 5-field cron only (Quartz requires Tangram OS)"
            ),
        }
        report["capabilities"]["notifications"] = {
            "state": "emulated",
            "detail": (
                "developer desktop notification (macOS/Windows/Linux); "
                "member routing + email/Slack channels require Tangram OS"
            ),
        }
        report["runtime"] = {
            "state": "running",
            "kind": "local-source",
            "backendUrl": self.backend_url,
            "uiUrl": self.ui_url,
            "database": "local-postgresql" if self.database is not None else None,
        }
        return report

    async def call(self, id: str, arguments: Any) -> Any:
        return await self.app.call(id, arguments)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if getattr(self, "_preview_dir", None) is not None:
            from .cli_actions import clear_session

            clear_session(self._preview_dir)
        if self._actions_server is not None:
            self._actions_server.close()
        if self._ui_server is not None:
            self._ui_server.close()
        _stop_process(self.process)
        self._log_file.close()
        if self.database is not None:
            self.database.close()
        self._project_lock.close()

    def __enter__(self) -> "LocalAppSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class LocalSourceRuntime:
    """Start the package's canonical FastAPI source backend on loopback."""

    def __init__(
        self,
        *,
        python: str | Path | None = None,
        startup_timeout_seconds: float = 30.0,
        request_timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
        audit_path: str | Path | None = None,
        managed_environment: bool = True,
        policy: "AuthorizationPolicy | str | None" = None,
    ) -> None:
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        self.python = _resolve_python(python)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.environment = dict(environment or {})
        self.audit_path = audit_path
        self.managed_environment = managed_environment
        self.policy = policy

    def start(self, app: "TangramApp") -> LocalAppSession:
        if app.source_root is None:
            raise LocalRuntimeError(
                "local source execution requires an app loaded from a package"
            )
        unsupported, needs_postgres = _local_provider_plan(app)
        if unsupported:
            raise UnsupportedRequirementError(
                "local source runtime has no configured provider for: "
                + ", ".join(unsupported)
            )
        spec = BackendSpec.from_project(app.source_root)
        _verify_python(self.python)
        preview = _safe_preview_dir(spec.root)
        project_lock = _ProjectLock.acquire(preview / "runtime.lock")
        database: LocalPostgres | None = None
        try:
            if needs_postgres:
                database = start_postgres(
                    spec.root,
                    preview,
                    environment={**os.environ, **self.environment},
                    startup_timeout_seconds=min(self.startup_timeout_seconds, 15.0),
                )
                apply_migrations(database, load_migrations(spec.root))
            runtime_python = _ensure_backend_python(
                preview,
                self.python,
                managed=self.managed_environment,
            )
            sdk_dir = _stage_backend_sdk(preview)
            # Started before the backend so its URL + bearer token can be
            # injected; the session attaches after bind, and calls before
            # that answer 503 (the staged SDK retries briefly).
            from .local_actions import LocalActionsServer
            from .local_schedules import LocalScheduler

            actions_server = LocalActionsServer.start(
                scheduler=LocalScheduler(preview / "schedules.json")
            )
        except Exception:
            if database is not None:
                database.close()
            project_lock.close()
            raise
        log_path = preview / "backend.log"
        log_file = log_path.open("ab", buffering=0)
        port = _free_port()
        backend_url = f"http://127.0.0.1:{port}"
        environment = _runtime_environment(
            spec,
            port,
            self.environment,
            sdk_dir=sdk_dir,
            database=database,
            app_name=app.graph.package.id.rsplit("/", 1)[-1],
        )
        environment["TANGRAM_LOCAL_ACTIONS_URL"] = actions_server.url
        environment["TANGRAM_LOCAL_ACTIONS_TOKEN"] = actions_server.token
        command = [
            str(runtime_python),
            "-m",
            "uvicorn",
            "--app-dir",
            str(spec.source_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            f"{spec.entry}:app",
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=spec.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            actions_server.close()
            log_file.close()
            if database is not None:
                database.close()
            project_lock.close()
            raise LocalRuntimeError(
                f"could not start local Python backend: {error}"
            ) from error

        try:
            document = _await_openapi(
                process,
                backend_url,
                log_path,
                timeout_seconds=self.startup_timeout_seconds,
            )
            _verify_operations(app, document)
            bound = app.bind(
                backend=backend_url,
                policy=self.policy,
                audit_path=self.audit_path,
                timeout_seconds=self.request_timeout_seconds,
            )
            session = LocalAppSession(
                bound,
                backend_url=backend_url,
                process=process,
                log_path=log_path,
                log_file=log_file,
                database=database,
                project_lock=project_lock,
                request_timeout_seconds=self.request_timeout_seconds,
                actions_server=actions_server,
                audit_path=self.audit_path,
            )
            from .cli_actions import record_session

            record_session(preview, backend_url, process.pid)
            session._preview_dir = preview
            actions_server.attach(session)
            session._attach_ui(
                LocalUiServer.start(
                    session,
                    preview,
                    environment={**os.environ, **self.environment},
                    managed_environment=self.managed_environment,
                )
            )
            return session
        except Exception:
            actions_server.close()
            _stop_process(process)
            log_file.close()
            if database is not None:
                database.close()
            project_lock.close()
            raise


def _entry_module(value: str) -> str:
    normalized = value.removesuffix(".py").replace("/", ".").replace("\\", ".")
    if not _MODULE.fullmatch(normalized):
        raise LocalRuntimeError(f"invalid backend entry module {value!r}")
    return normalized


def _resolve_python(value: str | Path | None) -> Path:
    if value is None:
        explicit = os.environ.get("TANGRAM_LOCAL_PYTHON", "").strip()
        candidates = [explicit] if explicit else []
        candidates.extend(("python3.12", "python3"))
        candidates.extend(
            (
                "/opt/homebrew/bin/python3.12",
                "/opt/homebrew/opt/python@3.12/bin/python3.12",
                "/usr/local/bin/python3.12",
                "/usr/local/opt/python@3.12/bin/python3.12",
                "/opt/anaconda3/bin/python3.12",
                sys.executable,
            )
        )
        for candidate in candidates:
            located = shutil.which(candidate) if Path(candidate).name == candidate else candidate
            path = Path(located).resolve() if located else None
            if path is not None and _python_version(path) >= (3, 12):
                return path
        return Path(sys.executable).resolve()
    rendered = str(value)
    located = shutil.which(rendered) if Path(rendered).name == rendered else None
    return Path(located or rendered).resolve()


def _verify_python(python: Path) -> None:
    if not python.is_file() or not os.access(python, os.X_OK):
        raise LocalRuntimeError(f"Python interpreter {python} is not executable")
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalRuntimeError(
            f"could not inspect Python interpreter: {error}"
        ) from error
    try:
        major, minor = (int(item) for item in completed.stdout.strip().split(".", 1))
    except (TypeError, ValueError) as error:
        raise LocalRuntimeError(
            "could not determine Python interpreter version"
        ) from error
    if completed.returncode != 0 or (major, minor) < (3, 12):
        raise LocalRuntimeError(
            f"local backend runtime requires Python 3.12+; found {major}.{minor}"
        )


def _python_version(python: Path) -> tuple[int, int]:
    if not python.is_file() or not os.access(python, os.X_OK):
        return (0, 0)
    try:
        completed = subprocess.run(
            [str(python), "-c", "import sys; print(sys.version_info.major, sys.version_info.minor)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        major, minor = completed.stdout.strip().split()
        return (int(major), int(minor)) if completed.returncode == 0 else (0, 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return (0, 0)


def _string_array(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LocalRuntimeError(f"{path} must be an array of strings")
    return tuple(value)


def _safe_preview_dir(root: Path) -> Path:
    preview = root / ".preview"
    if preview.is_symlink():
        raise LocalRuntimeError(
            ".preview is a symlink; refusing to write runtime state"
        )
    try:
        preview.mkdir(parents=True, exist_ok=True)
        if not preview.resolve().is_relative_to(root.resolve()):
            raise LocalRuntimeError(".preview resolves outside the app project")
    except OSError as error:
        raise LocalRuntimeError(f"could not prepare .preview: {error}") from error
    return preview


class _ProjectLock:
    def __init__(self, file) -> None:
        self._file = file

    @classmethod
    def acquire(cls, path: Path) -> "_ProjectLock":
        try:
            file = path.open("a+b")
        except OSError as error:
            raise LocalRuntimeError(f"could not open local runtime lock: {error}") from error
        if os.name != "nt":
            import fcntl

            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                file.close()
                raise LocalRuntimeError(
                    "another local runtime session already owns this project"
                ) from error
        return cls(file)

    def close(self) -> None:
        if self._file.closed:
            return
        if os.name != "nt":
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._file.close()


def _ensure_backend_python(preview: Path, system: Path, *, managed: bool) -> Path:
    if not managed:
        return system
    venv = preview / "backend-venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".tangram-deps"
    wanted = "\n".join((f"interpreter={system}", *sorted(_BASE_PINS)))
    try:
        current = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        expected_interpreter = f"interpreter={system}"
        if python.is_file() and current.splitlines()[:1] != [expected_interpreter]:
            shutil.rmtree(venv)
        if not python.is_file():
            _run_checked(
                [str(system), "-m", "venv", str(venv)],
                timeout_seconds=120,
                label="creating the backend virtual environment",
            )
        refreshed = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        if refreshed != wanted:
            _run_checked(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    *_BASE_PINS,
                ],
                timeout_seconds=600,
                label="installing the Tangram backend runtime",
            )
            marker.write_text(wanted, encoding="utf-8")
    except OSError as error:
        raise LocalRuntimeError(f"could not prepare backend virtual environment: {error}") from error
    return python


def _stage_backend_sdk(preview: Path) -> Path:
    source = Path(__file__).with_name("backend_runtime_sdk.py")
    target_dir = preview / "backend-sdk"
    package = target_dir / "tangram"
    target = package / "__init__.py"
    try:
        package.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    except OSError as error:
        raise LocalRuntimeError(f"could not stage the Tangram backend SDK: {error}") from error
    return target_dir


def _run_checked(
    command: list[str], *, timeout_seconds: float, label: str
) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalRuntimeError(f"{label} failed: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-3000:]
        raise LocalRuntimeError(f"{label} failed: {detail}")


def _runtime_environment(
    spec: BackendSpec,
    port: int,
    explicit: Mapping[str, str],
    *,
    sdk_dir: Path,
    database: LocalPostgres | None,
    app_name: str,
) -> dict[str, str]:
    keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM"}
    environment = {key: value for key, value in os.environ.items() if key in keep}
    environment.update(explicit)
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(sdk_dir), str(spec.source_dir), existing_pythonpath) if item
    )
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TANGRAM_WORKSPACE": environment.get("TANGRAM_WORKSPACE", "local"),
            "TANGRAM_APP": environment.get("TANGRAM_APP", app_name),
            "TANGRAM_RUNTIME": spec.runtime,
            "TANGRAM_LOCAL_SOURCE_DIR": str(spec.source_dir),
            "TANGRAM_ENTRY": spec.entry,
            "TANGRAM_PORT": str(port),
        }
    )
    if database is not None:
        environment.update(database.environment())
    return environment


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _await_openapi(
    process: subprocess.Popen[bytes],
    backend_url: str,
    log_path: Path,
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + timeout_seconds
    last = "not ready"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalRuntimeError(
                "backend exited during startup:\n" + _log_tail(log_path)
            )
        try:
            with opener.open(backend_url + "/openapi.json", timeout=1.0) as response:
                payload = response.read(_MAX_OPENAPI_BYTES + 1)
                if len(payload) > _MAX_OPENAPI_BYTES:
                    raise LocalRuntimeError(
                        "served OpenAPI document exceeds size limit"
                    )
                document = json.loads(payload)
                if not isinstance(document, Mapping):
                    raise ValueError("document is not an object")
                return document
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            last = type(error).__name__
        time.sleep(0.1)
    raise LocalRuntimeError(
        f"backend did not become ready ({last}):\n" + _log_tail(log_path)
    )


def _verify_operations(app: "TangramApp", document: Mapping[str, Any]) -> None:
    operations: dict[str, tuple[str, str]] = {}
    duplicates: set[str] = set()
    paths = document.get("paths", {})
    if isinstance(paths, Mapping):
        for path, path_item in paths.items():
            if not isinstance(path_item, Mapping):
                continue
            for method, operation in path_item.items():
                if (
                    not isinstance(method, str)
                    or method.lower() not in _HTTP_METHODS
                    or not isinstance(operation, Mapping)
                ):
                    continue
                operation_id = operation.get("operationId")
                if isinstance(operation_id, str):
                    if operation_id in operations:
                        duplicates.add(operation_id)
                    operations[operation_id] = (method.upper(), str(path))
    if duplicates:
        raise BackendContractError(
            "served OpenAPI contains duplicate operationIds: "
            + ", ".join(sorted(duplicates))
        )
    required = {
        binding.operation_id: (binding.method, binding.path)
        for action in app.graph.actions
        for binding in action.bindings
    }
    missing = sorted(set(required) - set(operations))
    if missing:
        raise BackendContractError(
            "backend is running but does not serve declared operationIds: "
            + ", ".join(missing)
        )
    mismatches = sorted(
        operation_id
        for operation_id, expected in required.items()
        if operations[operation_id] != expected
    )
    if mismatches:
        raise BackendContractError(
            "served OpenAPI method/path differs from the compiled graph for: "
            + ", ".join(mismatches)
        )


def _local_provider_plan(app: "TangramApp") -> tuple[tuple[str, ...], bool]:
    requirements = app.graph.runtime_requirements
    unsupported: list[str] = []
    for category in ("settings", "secrets"):
        items = requirements.get(category, ())
        if not isinstance(items, (list, tuple)):
            continue
        names = [
            str(item.get("name", category))
            for item in items
            if isinstance(item, Mapping) and item.get("required", True)
        ]
        if names:
            unsupported.append(f"{category} ({', '.join(names)})")
    infrastructure = requirements.get("infrastructureClaims", ())
    required_claims = [
        item
        for item in infrastructure
        if isinstance(item, Mapping) and item.get("required", True)
    ] if isinstance(infrastructure, (list, tuple)) else []
    needs_postgres = False
    if required_claims:
        root = app.source_root
        dependencies = (
            root / "manifests/deployment/dependencies.pkl" if root is not None else None
        )
        try:
            source = dependencies.read_text(encoding="utf-8") if dependencies else ""
        except OSError:
            source = ""
        claim_types = set(re.findall(r"\binfra\.([A-Za-z][A-Za-z0-9]*Claim)\b", source))
        if claim_types == {"PostgresqlDatabaseClaim"}:
            needs_postgres = True
        else:
            names = ", ".join(
                str(item.get("name", "infrastructureClaims")) for item in required_claims
            )
            detail = ", ".join(sorted(claim_types)) or "unrecognized claims"
            unsupported.append(f"infrastructureClaims ({names}: {detail})")
    return tuple(unsupported), needs_postgres


def _log_tail(path: Path, lines: int = 50) -> str:
    try:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError:
        return "(no backend log)"


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
