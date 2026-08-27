# Security and operations

[Back to the SDK guide](sdk-guide.md)

This page documents SDK exceptions, trust boundaries, troubleshooting, current
limitations, and development verification.

## Exceptions

All public SDK exceptions derive from `TangramAppError`. Important categories:

| Exception | Meaning |
|---|---|
| `ManifestValidationError` | Structured validation failed when validity was required |
| `ManifestCompilationError` | A valid-looking package could not produce a graph |
| `PklEvaluationError` | Pkl evaluation failed |
| `CapabilityGraphError` | Graph JSON is malformed or unsupported |
| `CapabilityGraphStaleError` | Skill/graph integrity verification failed |
| `UnknownBindingError` | No binding matches the ID |
| `AmbiguousActionError` | Short action ID resolves to multiple bindings |
| `InputValidationError` | Arguments violate input schema |
| `OutputValidationError` | Backend result violates output schema |
| `PolicyDeniedError` | Authorization denied the call |
| `ConfirmationRequiredError` | Call requires unavailable/preauthorization confirmation |
| `RequestRenderError` | Validated arguments cannot render to transport |
| `HttpResponseError` | Backend returned non-success HTTP status |
| `UnsupportedRequirementError` | Required provider is unavailable |
| `LocalRuntimeError` | Local runtime setup, readiness, or shutdown failed |
| `BackendContractError` | Served OpenAPI differs from the compiled graph |

## Security boundaries

The standalone host enforces useful local controls, but it is not a sandbox or
a substitute for Tangram OS isolation.

Implemented controls include:

- restricted Pkl evaluation rooted at the package;
- immutable graph models and package digests;
- input and output validation;
- read-only default agent policy;
- explicit confirmation behavior in the browser UI;
- loopback-only runtime listeners;
- disabled redirects and ambient HTTP proxies;
- fixed-header precedence over arguments;
- contained non-symlink `.preview` state;
- minimal backend environment;
- bounded HTTP payloads and logs; and
- metadata-only local audit records.

Important trust decisions and limitations:

- running local source executes selected app Python and JavaScript on the host;
- no container or OS sandbox is implied;
- local PostgreSQL uses loopback trust authentication for project-scoped dev
  data;
- local JSONL audit is not tamper-proof;
- OAuth, platform IAM, tenancy, and platform-grade secrets are not emulated;
- the UI host currently serves a top-level loopback page rather than a full
  Tangram OS iframe/tenant boundary; and
- deployment images are never executed as a fallback.

## Troubleshooting

### `pkl` was not found

Install Pkl or configure the evaluator explicitly. Compiled graph operations
do not need Pkl.

### `unsupported_requirement`

Inspect the capability report:

```sh
python -m tangram_app inspect . --tools
```

The standalone source runtime currently provides the canonical PostgreSQL
claim. Required settings, secrets, other infrastructure claims, OAuth, or an
Agent runtime can block execution.

### No local PostgreSQL runtime found

Install PostgreSQL or point to its binaries:

```sh
export TANGRAM_LOCAL_PG_BIN_DIR=/opt/homebrew/opt/postgresql@16/bin
```

### Migration changed after it was applied

Do not edit applied migrations. Add `V<n+1>__description.sql`. To discard local
development data deliberately, stop the app and remove `.preview/pgdata`.

### Backend did not become ready

Read `.preview/backend.log`. Confirm:

- the selected interpreter is Python 3.12+;
- `[tool.tangram.backend].entry` matches a module under `src/`;
- the module exports FastAPI `app`;
- imports are available in the managed runtime catalog; and
- `/openapi.json` can be generated during startup.

### Backend contract mismatch

The live FastAPI `operation_id`, HTTP method, or path differs from the manifest
mapping/OpenAPI package. Fix the source or regenerate the packaged OpenAPI so
all three agree.

### UI toolchain unavailable

Install Node/npm for managed first-run setup, or provide:

```sh
export TANGRAM_ESBUILD_BIN=/absolute/path/to/esbuild
export TANGRAM_COMPONENT_NODE_MODULES=/absolute/path/to/node_modules
```

### Another local runtime owns the project

Only one Python SDK session may own a project at once. Stop the existing
foreground `run` process before starting another.

### Generated skill is stale

Regenerate the skill into a new output directory after changing the package.
Do not edit its graph or lock manually.

## Current limitations

- Python compiler authority remains `development`.
- Scala/Tangram CLI validation remains authoritative for publishing.
- The source runtime supports canonical Python/FastAPI backends only.
- PostgreSQL is the only implemented infrastructure claim.
- Required standalone settings and secrets providers are not implemented.
- Backend storage, secrets, and nested action facades are not implemented.
- UI SQL query bindings and generic binding-style actions are stubbed.
- Local UI serving requires a Node toolchain on first setup unless an explicit
  compatible component toolchain is supplied.
- Programmatic CLI mutation grants and interactive agent confirmations are not
  implemented.
- MCP is not part of the SDK; generated skills and Python/CLI APIs are the
  agent integration surface.
- Remote Tangram OS execution/publishing is outside this package's current
  runtime driver.

## SDK development and verification

Run the complete test suite:

```sh
cd python-sdk
python -m unittest discover -s tests -p 'test_*.py'
```

Run lint and build a wheel:

```sh
ruff check src tests
uv build
```

Real-Pkl tests run when Pkl is available. Local PostgreSQL and browser
acceptance tests are separate environment-level checks; unit tests use bounded
fake backends/toolchains where appropriate.
