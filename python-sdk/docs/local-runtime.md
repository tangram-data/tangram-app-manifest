# Local app runtime

[Back to the SDK guide](sdk-guide.md)

This page covers running a frozen Tangram source package locally: its Python
backend, PostgreSQL claims and migrations, React UI, and HTTP boundary.

## Running source locally

`TangramApp.run_local()` and `TangramProject.run_local()` accept:

| Parameter | Default | Meaning |
|---|---:|---|
| `python` | discovered | Explicit Python 3.12+ interpreter |
| `startup_timeout_seconds` | `30` | Backend/PostgreSQL readiness deadline |
| `request_timeout_seconds` | `30` | Action HTTP deadline |
| `environment` | `{}` | Explicit runtime/toolchain environment overrides |
| `audit_path` | disabled | Metadata-only JSONL audit destination |
| `managed_environment` | `True` | Manage backend venv and UI component dependencies |
| `policy` | read-only local policy | Agent/Python action authorization policy |

Always use the returned session as a context manager or call `close()`:

```python
session = app.run_local()
try:
    print(session.capabilities())
finally:
    session.close()
```

## `LocalAppSession`

The active session exposes:

| Attribute or method | Meaning |
|---|---|
| `backend_url` | Loopback FastAPI URL |
| `ui_url` | Loopback root-component URL, or `None` |
| `log_path` | `.preview/backend.log` |
| `process` | Owned backend subprocess |
| `database` | Local PostgreSQL session, or `None` |
| `tools()` | Executable tool definitions |
| `capabilities()` | Runtime-aware capability report |
| `call(id, arguments)` | Governed asynchronous action invocation |
| `close()` | Idempotent runtime shutdown |

`close()` stops UI serving first, then the backend process group, then the
session-owned PostgreSQL process, and finally releases the project runtime
lock. Persistent `.preview` state is retained.

## Local runtime lifecycle

### Backend

The runtime parses
`manifests/deployment/source/backend/pyproject.toml` with Python's TOML parser.
The only required TOML declaration is the backend entry module:

```toml
[tool.tangram.backend]
entry = "main"
```

The complete supported shape is:

```toml
[project]
name = "backend"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.tangram.backend]
runtime = "python-3.12"
entry = "main"
egress = []
```

`runtime` is optional and defaults to `python-3.12`; that is currently the
only accepted value. `egress` and `[project].dependencies` are optional arrays
of strings. The runtime does not require or inspect the other `[project]`
metadata shown in the example.

The entry must resolve to a Python module below `src/`. The runtime:

1. acquires `.preview/runtime.lock`;
2. prepares `.preview/backend-venv` from the Tangram backend pins;
3. stages the app-backend `tangram` module under `.preview/backend-sdk`;
4. launches `python -m uvicorn` on `127.0.0.1`;
5. writes output to `.preview/backend.log`;
6. waits for `/openapi.json`; and
7. verifies every compiled operation ID, method, and path against the served
   document.

The backend receives a scrubbed process environment containing basic process
variables, explicit overrides, and the documented `TANGRAM_*` contract. It
does not inherit arbitrary cloud credentials or desktop tokens.

### PostgreSQL provider

The current infrastructure provider recognizes a canonical
`PostgresqlDatabaseClaim` in `manifests/deployment/dependencies.pkl`. Other
required infrastructure claims block startup before app code runs.

PostgreSQL state lives at `.preview/pgdata`. The runtime binds loopback with
local trust authentication and injects:

- `TANGRAM_DB_HOST=127.0.0.1`;
- `TANGRAM_DB_PORT=<allocated-port>`;
- `TANGRAM_DB_NAME=app`;
- `TANGRAM_DB_USER=tangram`; and
- `TANGRAM_DB_PASSWORD=tangram`.

Migration files must match `V<positive-integer>__<name>.sql` under
`manifests/deployment/migrations/main/`. They run in numeric order and each is
committed with its checksum record in `_tangram_app_migrations`.

An already-applied migration may not change. Add a new migration. For a
deliberate from-scratch local rebuild, stop the runtime and remove or move
`.preview/pgdata`; this deletes local app data.

### Backend `tangram` module

Local backend code can use:

```python
import tangram

rows = tangram.db.query(
    "select id, title from todos where id = %(id)s",
    {"id": 42},
)
tangram.db.execute(
    "update todos set title = %(title)s where id = %(id)s",
    {"id": 42, "title": "Updated"},
)
```

`tangram.context(request)` returns local workspace/app identity and consumes
trusted Tangram context headers when present.

The standalone staged backend module currently supports database access and
context. Backend `tangram.storage`, `tangram.secrets`, and nested
`tangram.actions` calls fail explicitly because their standalone providers are
not implemented yet.

### React UI provider

For a sandboxed root UI component, the runtime:

1. resolves the entry below `manifests/ui/components/`;
2. rejects symlinked source files;
3. discovers an explicit or nearby esbuild/component toolchain;
4. otherwise installs the compatible component catalog under
   `.preview/ui-runtime`;
5. bundles the component as browser IIFE JavaScript and optional CSS;
6. starts a loopback HTTP server with a restrictive CSP; and
7. injects `window.tangram`.

The managed component catalog currently includes React 18, ReactDOM 18, Ant
Design 5, Ant Design Icons 5, Day.js 1, Recharts 2, and esbuild 0.27.7.

The browser bridge supplies `getInputs`, `onInput`, `getTheme`, `onTheme`,
`query`, `action`, `performAction`, `emit`, `openUrl`, and `provideState`.
`performAction` is live. Query bindings and the generic binding-style `action`
surface are currently stubs in the Python host.

## HTTP drivers and network constraints

`LocalHttpDriver` is a loopback-only `HttpExecutionDriver`. The HTTP path:

- accepts only HTTP(S) base URLs;
- rejects userinfo and fragments;
- rejects non-loopback destinations by default;
- disables redirects;
- disables ambient proxy configuration;
- bounds URL, headers, request body, and response body;
- expects JSON responses; and
- redacts upstream response bodies from raised HTTP errors.

Default bounds are:

| Limit | Default |
|---|---:|
| URL | 8 KiB |
| Headers | 16 KiB |
| Request body | 1 MiB |
| Response body | 4 MiB |

`HttpExecutionDriver(allow_remote=True)` is an explicit trust decision. It
does not turn manifest-provided descriptions or endpoints into trusted input.
