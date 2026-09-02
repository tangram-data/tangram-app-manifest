# Tangram App SDK for Python

The SDK documentation starts at [docs/sdk-guide.md](docs/sdk-guide.md). This
README provides the shorter repository overview and common examples.

This directory contains the first authoring and standalone runtime SDK layer for the
[Tangram App Manifest](../spec/README.md). It evaluates a real manifest package
with Pkl, decodes the result into Python dataclasses mirroring the public Scala
domain model, and separately loads compiled Tangram Capability Graphs for the
transport-independent action host.

The SDK is deliberately small. Its only Python dependency is PyYAML for
OpenAPI documents authored as YAML. It currently provides:

- immutable, validated capability-graph models;
- `Application`, `AppResourceTypeDefinition`, `AppResourceTypeVersion`,
  `ResourceTypeAction`, `OpenApiMapping`, extractor, role, and configuration
  dataclasses corresponding to the Scala manifest-facing classes;
- a restricted Python-driven `pkl eval` integration and standard package-entry
  point loader;
- canonical action and binding lookup;
- JSON Schema input validation for the graph's supported schema subset;
- a read-only-by-default local development policy;
- one invocation path shared by future CLI, MCP, and HTTP adapters;
- pluggable execution drivers; and
- metadata-only audit events that do not record arguments or results.
- a `TangramApp` facade for package/graph discovery and local binding;
- a versioned JSON command surface through `python -m tangram_app` and
  `tangram-app`; and
- integrity-locked portable agent-skill generation for shell-capable agents;
- a `TangramProject` facade for AI-assisted source validation and compilation;
- canonical `[tool.tangram.backend]` parsing and host-native FastAPI source
  supervision; and
- a cached per-project Python 3.12 environment and staged backend `tangram`
  module;
- a loopback per-project PostgreSQL provider with persistent `.preview/pgdata`
  and checksum-ledgered migrations; and
- managed React component dependencies, esbuild compilation, and a loopback UI
  host with a browser `window.tangram` bridge; and
- source-mode local calls that verify the live `/openapi.json` contract before
  exposing any action.

It now compiles validated App and Connector mappings into a capability graph
and executes those bindings through a bounded HTTP driver, but does **not** yet
expose MCP. Agent skills use the Python command surface instead; protocol
adapters remain optional. The Tangram validator/compiler remains the publishing conformance authority;
Python's Pkl loader provides direct access to the manifest model and its draft
compiler derives the versioned runtime graph from the same evaluated values.
The graph wire contract is published as
[`schema/capability-graph.schema.json`](schema/capability-graph.schema.json).

## Loading a manifest package

Install a compatible [Pkl CLI](https://pkl-lang.org/) and load the directory
that contains `manifests/`:

```python
from tangram_app import PklManifestLoader

package = PklManifestLoader().load(".")

print(package.application.id)
orders = package.resource_type("Order")
list_orders = orders.active.action("List")
print(list_orders.effect, list_orders.all_open_api_mappings)
```

The evaluator roots file access at the package directory, permits dependency
aliases declared by `PklProject`, rejects direct `package:` imports, and denies
ambient environment and author-defined external-property reads. Resolving a
declared dependency may still populate Pkl's package cache from its pinned
package URI; run with a pre-populated cache when validation must make no network
requests. Deployment expressions that require platform-injected properties are
outside this initial package-model loader.

## Validating a package

`validate_manifest` returns structured findings instead of throwing on normal
manifest errors:

```python
from tangram_app import validate_manifest

result = validate_manifest(".")

for finding in result.findings:
    print(finding.severity.value, finding.code, finding.path, finding.message)

package = result.require_valid()
```

Validation currently covers required layout and dependency locks, Pkl loading,
application metadata, settings and secrets, resource/version/action uniqueness,
required action semantics, OpenAPI 3 parsing, operation uniqueness, action
mapping and extractor resolution, Agent tool and skill shapes, Connector
endpoint/OAuth safety, application-type deployment restrictions, and core UI
component/path checks. Pkl schema constraints remain the first validation
layer; these checks handle relationships across evaluated files. App deployment
inputs and integration modules currently produce explicit coverage warnings;
use `tangram app manifest validate` for their authoritative schema-reflection
checks.

## Compiling a capability graph

Compilation validates the package, selects active resource-type versions,
resolves mapped OpenAPI operations, flattens agent-facing inputs, records their
reverse path/query/header/body bindings, and calculates a deterministic digest
over `manifests/` and `libs/`:

```python
from tangram_app import compile_manifest

compiled = compile_manifest(".")
graph = compiled.graph

print(graph.package.digest)
print(graph.actions[0].bindings[0].input_schema)
graph.write_file("dist/tangram-app.json")
```

Only actions with `openApiMapping` or `openApiMappings` become executable graph
capabilities. Compilation findings retain validation warnings, including the
current deployment and integration coverage limits.

## Using the facade and command line

`TangramApp.from_package` compiles source through Pkl. `from_graph` loads a
precompiled snapshot without Pkl. Runtime backend configuration is applied
separately with `bind`:

```python
from tangram_app import TangramApp

app = TangramApp.from_package(".")
print([tool.id for tool in app.tools()])

bound = app.bind(backend="http://127.0.0.1:8000")
result = await bound.call("com.example/orders#Order.List@listOrders", {})
```

The Python package also exposes a machine-readable command interface. Every
command writes exactly one versioned JSON envelope to stdout:

```sh
python -m tangram_app build . --output dist/tangram-app.json
python -m tangram_app inspect . --tools --format json
printf '%s' '{}' | python -m tangram_app call \
  dist/tangram-app.json \
  com.example/orders#Order.List@listOrders \
  --backend http://127.0.0.1:8000 \
  --input-json -
```

Python compiler results are labeled `authority: development` until shared
golden fixtures prove parity with Tangram's publishing validator. That authority,
plus a `developmentOnly` marker for local Pkl project dependencies, is persisted
in the graph so loading a snapshot cannot accidentally upgrade its provenance.
Source-mode inspection reports manifest/OpenAPI validation as enforced;
snapshot-mode inspection reports it as delegated to the compiled artifact.

Runtime requirements are also part of the graph. Settings, secrets, and
deployment claims owned by an already-running App service are reported as
delegated to that backend. Required Connector or Agent requirements that this
standalone host cannot resolve block `bind()` before any network request.

## Authoring and running an agent-built app

`TangramProject` represents the mutable canonical source package used by a
coding agent or app author:

```python
from tangram_app import TangramProject

project = TangramProject.open(".")
validation = project.validate()
app = project.compile()

with project.run_local() as running:
    result = await running.call("com.example/todos#Todo.List@listTodos", {})
```

The local runtime reads
`manifests/deployment/source/backend/pyproject.toml`, resolves the declared
Python 3.12 entry module under `src/`, prepares a cached
`.preview/backend-venv` from the Tangram runtime pins, stages the backend
`tangram` module, starts `entry:app` with uvicorn on loopback, waits for
`/openapi.json`, and refuses startup when a compiled action operation is
missing. It executes the source tree, never the deployment image.

When `deployment/dependencies.pkl` declares the canonical `main`
`PostgresqlDatabaseClaim`, the SDK discovers a host PostgreSQL installation,
starts a project-scoped instance backed by `.preview/pgdata`, creates the
`app` database, and applies `deployment/migrations/main/Vn__*.sql` in order.
Applied checksums are recorded in `_tangram_app_migrations`; changing an
already-applied migration is refused. Install PostgreSQL separately or set
`TANGRAM_LOCAL_PG_BIN_DIR` to its `bin` directory. Other infrastructure claims,
settings, and secrets remain explicit unsupported requirements.

The local host is read-only by default. Embedded callers can pass a
`LocalDevelopmentPolicy` to `run_local()` to grant specific mutating action IDs
and, separately, preauthorize confirmation-gated actions.

For one governed call, the CLI can own the backend lifecycle automatically:

```sh
printf '%s' '{}' | python -m tangram_app call . \
  com.example/todos#Todo.List@listTodos --local --input-json -
```

For a long-running local backend session:

```sh
python -m tangram_app run .
```

The command emits one readiness envelope and stays in the foreground until
interrupted. It captures backend output in the contained `.preview` directory
and terminates the backend process group on shutdown.

Capability graphs retain declared root UI metadata, and `run_local()` compiles
the sandboxed root component and exposes it through `running.ui_url`. The first
run prepares `.preview/ui-runtime` with the server-compatible component catalog
and esbuild; callers may instead set `TANGRAM_ESBUILD_BIN` and
`TANGRAM_COMPONENT_NODE_MODULES`. The injected browser SDK routes
`performAction` to the same validated backend action host. Reversible UI actions
run directly; irreversible or confirmation-required actions require a browser
confirmation before the exact frozen request is retried.

## Installing the authoring skills

The SDK bundles two authoring skills: `tangram-app-builder` (apps with
their own backend/database/UI: layout, manifest templates, the
validate/run/call loop, platform-parity gotchas) and
`tangram-connector-builder` (connectors mapping an external SaaS API —
Gmail/Slack style — into governed actions with platform-managed OAuth).
Install either where your agent discovers instructions:

```sh
python -m tangram_app skill install NAME --project .   # ./.claude/skills/  (Claude Code, this repo)
python -m tangram_app skill install NAME --user        # ~/.claude/skills/  (Claude Code, all repos)
python -m tangram_app skill install NAME --codex       # ~/.codex/prompts/  (Codex, no Claude needed)
```

`skill install-builder` remains an alias for
`skill install tangram-app-builder`.

Claude Code users can instead install it as a plugin without the SDK:
`/plugin marketplace add tangram-data/tangram-app-manifest` then
`/plugin install tangram-app-builder`.

For **Codex-only** machines, `--codex` installs the full skill as the
custom prompt `/tangram-app-builder`. To make Codex apply it automatically
instead of on invocation, add to `~/.codex/AGENTS.md`:

```markdown
When asked to create, modify, or debug a Tangram app, first read
~/.codex/prompts/tangram-app-builder.md and follow it.
```

The packaged copy is release-authoritative; the repo mirrors are
sha-checked against it in CI.

## Prerequisites

```sh
pip install tangram-app-sdk
tangram-app doctor --fix     # diagnose everything; auto-install the Pkl CLI
```

`doctor` reports each prerequisite with an actionable hint. `--fix`
installs what is safe to install without sudo (the Pkl CLI, into
`~/.tangram/bin/`; the SDK finds it there with no PATH changes).
PostgreSQL (`initdb`/`pg_ctl`, only for database-claim apps) and Node
(only for UI components) are diagnosed with the exact package-manager
command for your platform.

## Installing and opening apps locally

Agents (or humans) can install a built app — a generated source package,
or an App Hub tarball — into the user-level store at `~/.tangram/apps/`,
then address it by id everywhere a package path is accepted:

```sh
tangram-app app install ./my-app            # or app.tar.gz, or an https URL
tangram-app app list
tangram-app open my-app                     # run + open the UI in a browser
printf '{}' | tangram-app call my-app 'com.example/my-app#Todo.List@listTodos' \
  --local --input-json -
tangram-app app uninstall my-app
```

`install` validates the package first and refuses stale layouts with
actionable findings. Runtime state stays under the installed app's own
`.preview/`. Override the store root with `TANGRAM_HOME`.

## Generating an agent skill

Generate a portable, graph-backed skill for Codex, Claude, or another
shell-capable agent:

```sh
python -m tangram_app skill generate . \
  --output dist/agent-skills/orders
```

The artifact contains a concise `SKILL.md`, optional Codex UI metadata, a
relative-path runner, a compact action catalog, an executable capability-graph
snapshot, and `skill.lock.json`. The runner verifies the graph against the lock
before every inspection or call and imports the installed Python SDK directly;
it does not require the native Tangram CLI or Pkl at invocation time.
When the canonical source package is available, the runner also accepts
`--local-package <app-dir>`: it recompiles the source, verifies its digest still
matches the skill snapshot, starts the declared Python backend, invokes the
action, and shuts the backend down. This source mode requires Pkl.

## Calling a local HTTP backend

`LocalHttpDriver` reverses the graph's flat agent inputs into OpenAPI path,
query, header, and JSON-body inputs. It is loopback-only, disables redirects
and ambient proxies, bounds URLs/headers/bodies/responses, and redacts upstream
error bodies:

```python
import asyncio

from tangram_app import LocalHttpDriver, TangramHost, compile_manifest


async def main() -> None:
    graph = compile_manifest(".").graph
    host = TangramHost(
        graph,
        driver=LocalHttpDriver("http://127.0.0.1:8000"),
    )
    result = await host.call(
        "com.example/orders#Order.List",
        {"status": "pending"},
    )
    print(result)


asyncio.run(main())
```

`HttpExecutionDriver(..., allow_remote=True)` is the explicit remote connector
escape hatch. Only use it with a trusted configured base URL; redirects remain
disabled and configured headers are injected after agent-provided inputs so
credentials cannot be overridden.

## Development

Create an isolated environment and install the SDK. The real-Pkl integration
tests run when `pkl` is installed and are otherwise skipped:

```sh
cd python-sdk
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Example

```python
import asyncio

from tangram_app import (
    CapabilityGraph,
    InMemoryDriver,
    LocalDevelopmentPolicy,
    TangramHost,
)


async def main() -> None:
    graph = CapabilityGraph.from_file("dist/tangram-app.json")
    driver = InMemoryDriver()

    @driver.handler("com.example/orders#Order.List@listOrders")
    async def list_orders(arguments):
        return {"orders": [], "status": arguments.get("status")}

    host = TangramHost(
        graph,
        driver=driver,
        policy=LocalDevelopmentPolicy(),
    )
    result = await host.call(
        "com.example/orders#Order.List@listOrders",
        {"status": "pending"},
    )
    print(result)


asyncio.run(main())
```

`LocalDevelopmentPolicy` allows `Stateless` actions and denies mutations. A
mutating binding must be named explicitly in `allow_mutations`; an action that
requires confirmation must additionally be named in
`preauthorized_confirmations`. This is startup pre-authorization, not a claim
that a generic agent protocol supplied per-call human confirmation.
