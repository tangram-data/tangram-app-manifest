# Tangram App SDK for Python

This guide describes the current Python SDK in `python-sdk/`. It covers both
halves of the SDK:

- authoring: load, inspect, validate, and compile a canonical Tangram app
  package; and
- execution: run an agent-built app locally, expose its actions to Python or
  shell-capable agents, and serve its declared React UI without Tangram OS.

The SDK does not replace Tangram OS. It provides a host-native development and
personal-use runtime with explicit local guarantees. Publishing and platform
installation remain governed by Tangram's authoritative Scala validator and
Tangram OS.

## Status and compatibility

The package is currently `0.1.0.dev0` and requires Python 3.11 or newer. Local
agent-built Python backends declare the `python-3.12` runtime and therefore
require a Python 3.12-or-newer interpreter.

Capability graphs emitted by the Python compiler have `formatVersion: "1"`
and `authority: "development"`. The development authority is intentional: the
Python compiler is useful for local execution and agent integration, but the
Scala manifest validator remains the publishing conformance authority until
shared golden tests establish full parity.

The SDK's install-time Python dependency is PyYAML. Source-package operations
also use external tools according to the selected workflow:

| Workflow | Required software |
|---|---|
| Load, validate, or compile source manifests | Pkl CLI |
| Inspect or invoke a compiled capability graph | Python only |
| Run a local Python backend | Python 3.12+; the SDK creates the app venv |
| Run an app with `PostgresqlDatabaseClaim` | Host PostgreSQL toolchain |
| Serve a sandboxed React UI | Node/npm on first setup, or an explicit esbuild/component toolchain |

## Installation

For development from this repository:

```sh
cd python-sdk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Install the Pkl CLI before working from manifest source. Confirm the SDK and
Pkl are visible:

```sh
python -m tangram_app --help
pkl --version
```

For local database-backed apps, install PostgreSQL. The SDK discovers
`initdb`, `postgres`, `pg_ctl`, and `psql` from, in order:

1. `TANGRAM_LOCAL_PG_BIN_DIR`;
2. `PATH`; and
3. Homebrew PostgreSQL kegs under `/opt/homebrew/opt` or `/usr/local/opt`.

For example, on macOS:

```sh
brew install postgresql@16
```

For local React UI serving, install Node.js/npm or provide both
`TANGRAM_ESBUILD_BIN` and `TANGRAM_COMPONENT_NODE_MODULES`.

## Canonical package layout

The SDK opens the directory above `manifests/`:

```text
my-app/
└── manifests/
    ├── PklProject
    ├── PklProject.deps.json
    ├── app.pkl
    ├── api/
    │   ├── resources.pkl
    │   ├── spec.pkl
    │   └── open-api.json
    ├── deployment/
    │   ├── components.pkl
    │   ├── dependencies.pkl
    │   ├── migrations/main/V1__initial.sql
    │   └── source/backend/
    │       ├── pyproject.toml
    │       └── src/main.py
    └── ui/
        ├── spec.pkl
        └── components/root/index.tsx
```

The minimum validation layout is:

- `manifests/PklProject`;
- `manifests/PklProject.deps.json`;
- `manifests/app.pkl`; and
- `manifests/api/resources.pkl`.

Backend, deployment, migrations, and UI files are required only when the app
declares those capabilities.

Runtime state is written under the app's `.preview/` directory. It is never
part of the manifest package or capability digest.

## Mental model

The main data flow is:

```text
Pkl manifests + OpenAPI
        │
        ├── PklManifestLoader ──> manifest dataclasses
        │
        └── validate_manifest ──> ManifestCompiler ──> CapabilityGraph
                                                        │
                          TangramProject / TangramApp ───┤
                                                        │
                         policy + audit + driver ──> TangramHost.call
                                                        │
                    local backend / HTTP backend / in-memory handler
```

The manifest model preserves authoring concepts such as applications,
resource types, versions, actions, roles, privileges, mappings, extractors,
settings, and secrets. The capability graph is the smaller executable
projection consumed by hosts and generated agent skills.

## Five-minute local workflow

Validate and inspect a package:

```sh
python -m tangram_app validate /absolute/path/to/my-app
python -m tangram_app inspect /absolute/path/to/my-app --tools
```

Run the backend, database, and UI as one foreground-owned session:

```sh
python -m tangram_app run /absolute/path/to/my-app
```

The readiness envelope contains `backendUrl`, optional `uiUrl`, the backend log
path, and package identity. Open `uiUrl` in a browser. Press Ctrl-C to stop the
UI server, backend, and PostgreSQL process. Cached dependencies and database
data remain under `.preview/` for the next start.

Invoke one Stateless action and stop automatically:

```sh
printf '%s' '{}' | python -m tangram_app call \
  /absolute/path/to/my-app \
  'com.example/my-app#Todo.List@listTodos' \
  --local \
  --input-json -
```

## In-backend `tangram` module

Backend code (platform or standalone) imports one staged `tangram` module.
The public surface:

| Surface | What it does | Standalone |
|---|---|---|
| `tangram.db` / `tangram.context` | Own-database SQL, invocation identity | yes |
| `tangram.actions.invoke(...)` | Call the app's (or a declared dependency's) actions | own app only |
| `tangram.storage`, `tangram.secrets` | Own object storage, declared secrets | no |
| `tangram.sql.run(name, params)` | Declared workspace queries (`declare_backend_query`) | no |
| `tangram.schedules` | Durable schedules firing the app's own unattended actions (`declare_backend_scheduling`) | no |
| `tangram.notifications` | Notify workspace members by account id via platform channels (`declare_backend_notifications`) | desktop notification |

Platform-only surfaces raise the module's structured unsupported error when
called standalone; `tangram.notifications` instead delivers a native
desktop notification on the developer's machine (macOS/Windows/Linux) with
platform-shaped envelopes, so notification flows are testable locally.
The two newest surfaces in brief — `schedules.create`
upserts by name with exactly one of `every` (`"30m"`-style interval), `cron`
(5-field, evaluated in `timezone`), or `at` (one-shot ISO instant), plus
`list`/`pause`/`resume`/`delete`/`runs`; `notifications.send(to, subject,
body, link?, channel?, dedupe_key?)` addresses member account ids only
(never raw email/Slack addresses), delivers asynchronously at-most-once,
and `notifications.list` shows per-recipient terminal status. Normative
operation shapes, capability gates, and delivery semantics live in the
[SDK ↔ host ABI](sdk-host-abi.md).

## Detailed documentation

- [Manifest authoring and compilation](manifest-authoring.md)
- [Local app runtime](local-runtime.md)
- [Actions and agent integration](actions-and-agents.md)
- [Command-line reference](cli-reference.md)
- [Security and operations](security-and-operations.md)
- [SDK ↔ host ABI](sdk-host-abi.md)

Each topic has one owning page so normative behavior is not duplicated.
