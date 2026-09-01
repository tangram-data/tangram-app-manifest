---
name: tangram-app-builder
description: Build and test a Tangram app end-to-end on this machine with the Python SDK — scaffold the manifest package, author Pkl + OpenAPI + FastAPI backend, then drive the validate → run → call loop. Use when asked to create, modify, or debug a Tangram app, its manifests, actions, backend, or local runtime behavior — or to install, run, open, or invoke a built Tangram app locally (the ~/.tangram user-level app store), all without a Tangram OS deployment.
---

# Build a Tangram app locally

A Tangram app is a **manifest package**: Pkl manifests declaring what the app
is, exposes, and needs, plus a canonical Python (FastAPI) backend and optional
React UI. The Python SDK (pip package `tangram-app-sdk`, module
`tangram_app`) validates, compiles, runs, and invokes it entirely on
loopback — no Tangram OS. This skill is self-contained: the templates below
are complete and verified; you do NOT need the tangram-app-manifest repo.
For a CONNECTOR to an external SaaS API (Gmail/Slack style: vendor OAuth,
no own backend) use the `tangram-connector-builder` skill instead.

## Prerequisites

Check before installing anything:

- SDK: `python3 -m tangram_app --help` (or `tangram-app --help`) already
  works in many environments. If missing: `pip install tangram-app-sdk`
  once it is on PyPI; until then install from a checkout or wheel of
  `github.com/tangram-data/tangram-app-manifest`
  (`pip install ./python-sdk` from the repo root). Backends themselves
  need Python 3.12+ on the machine; the SDK itself runs on 3.11+.
- Pkl CLI on PATH (`pkl --version`) — needed to load source manifests.
- Host PostgreSQL toolchain (`initdb`/`pg_ctl`) only if the app declares a
  database claim.

## Package layout

```text
my-app/
└── manifests/
    ├── PklProject              # depends on the published schema package
    ├── PklProject.deps.json    # generated: run `pkl project resolve` here
    ├── app.pkl                 # identity: group/name/version/appType
    ├── api/
    │   ├── resources.pkl       # resource types → versions → actions
    │   ├── spec.pkl            # apiSpecFile + backend {serviceName, port}
    │   └── open_api.yml        # OpenAPI 3.x; operationIds bind actions
    ├── settings.pkl            # declared settings (optional)
    ├── deployment/
    │   ├── dependencies.pkl    # infrastructure claims (optional)
    │   ├── source/backend/     # canonical Python backend
    │   │   ├── pyproject.toml  # [tool.tangram.backend] entry/runtime/egress
    │   │   └── src/<entry>.py  # exposes `app` (FastAPI)
    │   └── migrations/main/    # numbered .sql, applied in order (DB claim)
    └── ui/components/<name>/index.tsx   # sandboxed root component (optional)
```

Runtime state (venv, database, logs) lands in `my-app/.preview/` — never
commit it.

## Minimal manifests

Author against the TYPED schema package — Pkl then type-checks fields and
enum values at eval time instead of failing later in validation.
`manifests/PklProject`:

```pkl
amends "pkl:Project"

dependencies {
  ["tangram-app-manifest"] {
    uri = "package://pkg.pkl-lang.org/github.com/tangram-data/tangram-app-manifest/tangram-app-manifest@1.0.0"
  }
}
```

Then run `pkl project resolve` inside `manifests/` once (network on first
use; cached afterwards) to generate `PklProject.deps.json`.

`manifests/app.pkl` (top-level scalars, no class):

```pkl
manifestSpecVersion = "v1"
group = "com.example"
name = "orders"
version = "0.1.0"
appType = "App"
description = "Orders demo"
tags = List("demo")
```

`manifests/api/resources.pkl` — every executable action MUST have an
`openApiMapping` (actions without one validate but are not executable):

```pkl
import "@tangram-app-manifest/resources.pkl" as resources

types = List(
  new resources.ResourceTypeDefinition {
    name = "Order"
    doc = "An order"
    activeVersion = "v1"
    versions = List(
      new resources.ResourceTypeVersion {
        version = "v1"
        served = true
        actions = List(
          new resources.Action {
            name = "List"
            doc = "List orders"
            privilege = "Read"        // Read | Write | ...
            effect = "Stateless"      // ActionEffect: Stateless | Reversible | Irreversible
            idempotent = true
            openApiMapping = new resources.OpenApiMapping { operationId = "listOrders" }
          }
        )
        presetRoles = List(
          new resources.Role { name = "reader"; permissions = List("Read"); description = "Can list" }
        )
      }
    )
  }
)
```

`manifests/api/spec.pkl`:

```pkl
import "@tangram-app-manifest/api.pkl" as api

apiSpecFile = "open_api.yml"
backend = new api.ServiceBackend { serviceName = "orders"; port = 8080 }
```

(Schema-less `new Dynamic { ... }` blocks with the same fields also load,
but skip Pkl's type checking — prefer the typed classes above.)

`open_api.yml` declares the paths; each `operationId` referenced by an
`openApiMapping` must exist there — input/output schemas come from it.

Backend `pyproject.toml`:

```toml
[project]
name = "orders"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[tool.tangram.backend]
runtime = "python-3.12"
entry = "main"          # src/main.py must expose `app`
egress = []
```

The SDK launches uvicorn itself (own port, managed venv with pinned
fastapi/psycopg/etc.) — the entry module just exposes `app`. On start,
`run` checks every COMPILED ACTION BINDING (operationId + method + path)
against the live `/openapi.json` and refuses on mismatch; extra live
routes and declared-but-unmapped operations are fine. **Set `operation_id`
explicitly on every action-bound route** — FastAPI's auto-generated ids
(`list_orders_orders_get`) will not match the manifest; unmapped routes
may keep generated ids.

**Dependencies gotcha:** the managed venv installs only the SDK's pinned
base set (fastapi, pydantic, psycopg, requests, httpx, uvicorn…) —
`[project].dependencies` is *declared* metadata, not installed. Code that
imports anything beyond the pins needs a prepared interpreter via the
Python API (`run_local(..., managed_environment=False)`), or stick to the
pinned set.

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/orders", operation_id="listOrders")
def list_orders():
    return [{"id": 1, "status": "open"}]
```

## The loop

Every CLI command prints exactly one JSON envelope to stdout — parse it,
don't scrape logs.

```sh
python -m tangram_app validate my-app                 # structured findings
python -m tangram_app inspect my-app --tools          # actions as agent tools
python -m tangram_app run my-app                      # backend+db+ui until Ctrl-C
printf '%s' '{}' | python -m tangram_app call my-app \
  'com.example/orders#Order.List@listOrders' --local --input-json -
```

Action id format: `{group}/{name}#{ResourceType}.{Action}`; the binding id
appends `@{operationId}`.

Know these before debugging "failures" that are actually policy:

- **CLI default policy is read-only.** A mutating `call` returns
  `policy_denied` by design. Grant mutations via the Python API
  (`LocalDevelopmentPolicy(allow_mutations={action_id})` /
  `project.run_local(policy=...)`), not by weakening the manifest.
- **Irreversible or confirmation-gated actions refuse unattended** —
  from backend code (`tangram.actions.invoke`) and from schedules. That
  refusal is platform parity, not a bug.
- Postgres claim: declare it in `manifests/deployment/dependencies.pkl`
  (referencing `infra.PostgresqlDatabaseClaim` — the only claim with a
  local provider); migrations under
  `manifests/deployment/migrations/main/` apply on start.

## In-backend `tangram` module

Backend code imports `tangram` — same signatures locally and on Tangram OS
(conformance-pinned; exact request/response shapes and error codes are in
`docs/sdk-host-abi.md` of the tangram-app-manifest repo):

| Surface | Standalone behavior |
|---|---|
| `tangram.db.query/execute`, `tangram.context` | real (local Postgres) |
| `tangram.actions.invoke(rt, action, args)` | real, own app only, unattended-gated |
| `tangram.notifications.send/list` | native desktop notification; member routing is platform-only |
| `tangram.schedules.create/list/pause/resume/delete/runs` | host-side scheduler; fires only while `run` is up; Unix 5-field cron only |
| `tangram.storage`, `tangram.secrets`, `tangram.sql` | structured "requires Tangram OS" error |

Errors surface as `tangram.ActionError(code, message, retryable)` — branch
on `code`, never message text.

## Install and use locally (the app store)

"Install the app locally" means the USER-LEVEL APP STORE at `~/.tangram`
— it does NOT mean deploying into a Tangram OS instance. (The native
CLI's `tangram app pkg install --workspace ...` deploys to a running
Tangram OS; use that lane only when the ask names a workspace, an
instance, or "Tangram OS" explicitly.)

```sh
python -m tangram_app app install ./my-app     # validate + copy into ~/.tangram/apps/
python -m tangram_app app list
python -m tangram_app open my-app              # run + open the app UI in the browser
printf '{}' | python -m tangram_app call my-app \
  'com.example/my-app#Todo.List@listTodos' --local --input-json -
python -m tangram_app app uninstall my-app
```

Installed apps are addressable by app id (or unique bare name) everywhere
a package path is accepted; their runtime state lives under the installed
copy's own `.preview/`. `app install` also accepts a `.tar.gz`/`.zip` or
an https URL.

## Ship it

- `python -m tangram_app skill generate my-app --output dist/skills/orders`
  emits an integrity-locked consumer skill (agents discover + call actions;
  refuses on source drift).
- The Python compiler is `authority: "development"`. Publishing conformance
  is the Scala CLI (`TANGRAM_CLI.md`); run it before releasing.

## Going deeper

These live in the `tangram-data/tangram-app-manifest` repo (clone or browse
only if this skill doesn't answer the question): `python-sdk/docs/` —
`sdk-guide.md` (map), `manifest-authoring.md`, `local-runtime.md`,
`actions-and-agents.md` (policy/audit/skills), `cli-reference.md`
(envelope shape), `sdk-host-abi.md` (wire truth); manifest semantics in
`spec/*.md`; a seed package at `python-sdk/tests/fixtures/minimal-app/`.
