"""Per-project PostgreSQL lifecycle for the host-native app runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Mapping

from .errors import LocalRuntimeError


DB_NAME = "app"
DB_USER = "tangram"
_MIGRATION = re.compile(r"^V([1-9][0-9]*)__.+\.sql$")
_LEDGER_DDL = """create table if not exists _tangram_app_migrations(
  version int primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
)"""


@dataclass(frozen=True, slots=True)
class PostgresBinaries:
    initdb: Path
    postgres: Path
    pg_ctl: Path
    psql: Path

    @classmethod
    def discover(cls, environment: Mapping[str, str] | None = None) -> "PostgresBinaries":
        env = dict(os.environ if environment is None else environment)
        candidates: list[Path] = []
        explicit = env.get("TANGRAM_LOCAL_PG_BIN_DIR", "").strip()
        if explicit:
            candidates.append(Path(explicit))
        for directory in env.get("PATH", "").split(os.pathsep):
            if directory:
                candidates.append(Path(directory))
        for opt in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
            if opt.is_dir():
                candidates.extend(
                    item / "bin"
                    for item in sorted(opt.iterdir(), reverse=True)
                    if item.name.startswith("postgresql")
                )
        seen: set[Path] = set()
        for directory in candidates:
            directory = directory.expanduser()
            if directory in seen:
                continue
            seen.add(directory)
            paths = tuple(directory / name for name in ("initdb", "postgres", "pg_ctl", "psql"))
            if all(path.is_file() and os.access(path, os.X_OK) for path in paths):
                return cls(*paths)
        raise LocalRuntimeError(
            "no local PostgreSQL runtime found; install PostgreSQL "
            "(for example `brew install postgresql@16`) or set "
            "TANGRAM_LOCAL_PG_BIN_DIR"
        )


@dataclass(slots=True)
class LocalPostgres:
    """A loopback-only Postgres process with persistent project data."""

    process: subprocess.Popen[bytes]
    port: int
    data_dir: Path
    log_path: Path
    socket_dir: Path
    binaries: PostgresBinaries

    def environment(self) -> dict[str, str]:
        return {
            "TANGRAM_DB_HOST": "127.0.0.1",
            "TANGRAM_DB_PORT": str(self.port),
            "TANGRAM_DB_NAME": DB_NAME,
            "TANGRAM_DB_USER": DB_USER,
            # Local trust authentication ignores it; psycopg requires a value.
            "TANGRAM_DB_PASSWORD": DB_USER,
        }

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        shutil.rmtree(self.socket_dir, ignore_errors=True)


def start_postgres(
    root: Path,
    preview: Path,
    *,
    environment: Mapping[str, str] | None = None,
    startup_timeout_seconds: float = 15.0,
) -> LocalPostgres:
    binaries = PostgresBinaries.discover(environment)
    data_dir = preview / "pgdata"
    if data_dir.is_symlink():
        raise LocalRuntimeError(".preview/pgdata is a symlink; refusing database startup")
    fresh = not data_dir.is_dir()
    if fresh:
        try:
            _run_tool(
                [
                    str(binaries.initdb),
                    "-D",
                    str(data_dir),
                    "-U",
                    DB_USER,
                    "-A",
                    "trust",
                    "-E",
                    "UTF8",
                ],
                timeout_seconds=60,
                label="initdb",
            )
            (data_dir / ".tangram-initdb-ok").write_text("ok", encoding="utf-8")
        except OSError as error:
            if not (data_dir / ".tangram-initdb-ok").is_file():
                shutil.rmtree(data_dir, ignore_errors=True)
            raise LocalRuntimeError(f"could not record completed initdb: {error}") from error
        except LocalRuntimeError:
            if not (data_dir / ".tangram-initdb-ok").is_file():
                shutil.rmtree(data_dir, ignore_errors=True)
            raise
    elif not (data_dir / ".tangram-initdb-ok").is_file():
        raise LocalRuntimeError(
            "the local Postgres data directory has no completed-init marker; "
            "move or remove .preview/pgdata to rebuild it"
        )

    # A hard-killed SDK may leave its postmaster behind. The project runtime
    # lock prevents this from stopping another live SDK session.
    _stop_stale_postmaster(binaries, data_dir)
    port = _free_port()
    socket_dir = Path(tempfile.mkdtemp(prefix="tangram-pg-"))
    log_path = data_dir / "tangram-pg.log"
    log_file = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [
                str(binaries.postgres),
                "-D",
                str(data_dir),
                "-p",
                str(port),
                "-c",
                "listen_addresses=127.0.0.1",
                "-c",
                f"unix_socket_directories={socket_dir}",
                "-c",
                "lc_messages=C",
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise LocalRuntimeError(f"could not start PostgreSQL: {error}") from error
    finally:
        log_file.close()

    database = LocalPostgres(process, port, data_dir, log_path, socket_dir, binaries)
    try:
        _await_ready(database, timeout_seconds=startup_timeout_seconds)
        _ensure_database(database)
        return database
    except Exception:
        database.close()
        raise


def load_migrations(root: Path) -> tuple[tuple[int, str], ...]:
    directory = root / "manifests/deployment/migrations/main"
    if not directory.is_dir():
        return ()
    values: list[tuple[int, str]] = []
    versions: set[int] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        match = _MIGRATION.fullmatch(path.name)
        if match is None:
            raise LocalRuntimeError(f"invalid migration filename {path.name!r}")
        version = int(match.group(1))
        if version in versions:
            raise LocalRuntimeError(f"duplicate migration version V{version}")
        versions.add(version)
        try:
            values.append((version, path.read_text(encoding="utf-8")))
        except OSError as error:
            raise LocalRuntimeError(f"could not read migration {path.name}: {error}") from error
    return tuple(sorted(values))


def apply_migrations(database: LocalPostgres, migrations: tuple[tuple[int, str], ...]) -> None:
    _psql(database, DB_NAME, _LEDGER_DDL)
    applied_text = _psql(
        database,
        DB_NAME,
        "select version || ':' || checksum from _tangram_app_migrations order by version",
        tuples_only=True,
    )
    applied: dict[int, str] = {}
    for line in applied_text.splitlines():
        if ":" in line:
            version, checksum = line.strip().split(":", 1)
            applied[int(version)] = checksum
    for version, sql in migrations:
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        existing = applied.get(version)
        if existing == checksum:
            continue
        if existing is not None:
            raise LocalRuntimeError(
                f"migration V{version} changed after it was applied locally; "
                "add a new migration or remove .preview/pgdata for a from-scratch rebuild"
            )
        statement = (
            "begin;\n"
            + sql
            + "\ninsert into _tangram_app_migrations(version, checksum) values "
            + f"({version}, '{checksum}');\ncommit;\n"
        )
        try:
            _psql(database, DB_NAME, statement)
        except LocalRuntimeError as error:
            raise LocalRuntimeError(f"migration V{version} failed: {error}") from error


def _await_ready(database: LocalPostgres, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = "not ready"
    while time.monotonic() < deadline:
        if database.process.poll() is not None:
            raise LocalRuntimeError(
                "PostgreSQL exited during startup:\n" + _log_tail(database.log_path)
            )
        completed = _psql_run(database, "postgres", "show data_directory")
        if completed.returncode == 0:
            reported = completed.stdout.strip()
            try:
                if Path(reported).resolve() != database.data_dir.resolve():
                    raise LocalRuntimeError(
                        f"port {database.port} answered from a different PostgreSQL data directory"
                    )
            except OSError as error:
                raise LocalRuntimeError(f"could not verify PostgreSQL identity: {error}") from error
            return
        last = completed.stderr.strip()[-500:] or "connection refused"
        time.sleep(0.2)
    raise LocalRuntimeError(f"PostgreSQL did not become ready ({last})")


def _ensure_database(database: LocalPostgres) -> None:
    exists = _psql(
        database,
        "postgres",
        f"select 1 from pg_database where datname = '{DB_NAME}'",
        tuples_only=True,
    ).strip()
    if exists != "1":
        _psql(database, "postgres", f"create database {DB_NAME}")


def _psql(
    database: LocalPostgres,
    name: str,
    sql: str,
    *,
    tuples_only: bool = False,
) -> str:
    completed = _psql_run(database, name, sql, tuples_only=tuples_only)
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:] or completed.stdout.strip()[-2000:]
        raise LocalRuntimeError(f"psql failed: {detail}")
    return completed.stdout


def _psql_run(
    database: LocalPostgres,
    name: str,
    sql: str,
    *,
    tuples_only: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(database.binaries.psql),
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        "127.0.0.1",
        "-p",
        str(database.port),
        "-U",
        DB_USER,
        "-d",
        name,
    ]
    if tuples_only:
        command.extend(("-A", "-t"))
    environment = {**os.environ, "PGCONNECT_TIMEOUT": "2"}
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            input=sql,
            env=environment,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalRuntimeError(f"could not run psql: {error}") from error


def _stop_stale_postmaster(binaries: PostgresBinaries, data_dir: Path) -> None:
    if not (data_dir / "postmaster.pid").is_file():
        return
    try:
        status = subprocess.run(
            [str(binaries.pg_ctl), "-D", str(data_dir), "status"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        if status.returncode == 0:
            _run_tool(
                [str(binaries.pg_ctl), "-D", str(data_dir), "stop", "-m", "fast", "-w"],
                timeout_seconds=15,
                label="pg_ctl stop",
            )
    except (OSError, subprocess.TimeoutExpired):
        return


def _run_tool(command: list[str], *, timeout_seconds: float, label: str) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalRuntimeError(f"{label} could not run: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise LocalRuntimeError(f"{label} failed: {detail}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _log_tail(path: Path, lines: int = 50) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no PostgreSQL log)"
