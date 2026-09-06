# Actions and agent integration

[Back to the SDK guide](sdk-guide.md)

This page covers the compiled action surface, governed invocation, confirmation
policy, audit records, generated skills, and lower-level host embedding.

## `TangramApp`

`TangramApp` is a compiled app facade. Construct it from source or a graph:

```python
from tangram_app import TangramApp

source_app = TangramApp.from_package(".")
snapshot_app = TangramApp.from_graph("dist/tangram-app.json")
```

`from_package()` requires Pkl and retains source validation findings and the
source root. `from_graph()` needs no Pkl, but cannot start a source runtime.

Discovery methods:

- `tools()` returns one `ToolDefinition` per executable binding;
- `ui()` returns root UI metadata or `None`; and
- `capabilities()` reports support, delegated requirements, blocked actions,
  authority, and development-only status.

An action can have multiple OpenAPI mappings, so its action ID can be
ambiguous. Prefer the canonical binding ID returned by `tools()`:

```text
<package-id>#<resource-type>.<action>@<operation-id>
```

For example:

```text
com.example/todos#Todo.List@listTodos
```

The shorter action ID is accepted only when it resolves to exactly one
binding.

## Binding an already-running backend

Use `bind()` when an app backend already runs on loopback:

```python
import asyncio

from tangram_app import TangramApp

app = TangramApp.from_graph("dist/tangram-app.json")
bound = app.bind(
    backend="http://127.0.0.1:8710",
    timeout_seconds=30,
    audit_path=".preview/invocations.jsonl",
)

result = asyncio.run(
    bound.call("com.example/todos#Todo.List@listTodos", {})
)
```

`bind()` accepts:

| Parameter | Meaning |
|---|---|
| `backend` | Required loopback base URL |
| `policy` | `AuthorizationPolicy`, `"local-development"`, or `None` |
| `audit` | Custom `AuditSink` |
| `audit_path` | Local JSONL diagnostic audit file |
| `headers` | Fixed backend headers; agent inputs cannot override them |
| `timeout_seconds` | Per-call HTTP timeout |

Configure either `audit` or `audit_path`, not both.

`TangramApp.bind()` intentionally uses `LocalHttpDriver`, so remote destinations
are rejected. Use `HttpExecutionDriver(..., allow_remote=True)` through a
lower-level `TangramHost` only for an explicitly trusted remote endpoint.

## Invocation pipeline

Every `TangramHost.call()` follows the same order:

1. resolve the action/binding ID;
2. ensure arguments are JSON serializable and calculate their audit hash;
3. validate arguments against the compiled input schema;
4. ask the authorization policy;
5. render and execute through the selected driver;
6. validate the result against the output schema; and
7. record an audit event.

No CLI, generated skill, UI bridge, or HTTP adapter should reimplement these
steps.

### Input projection

Agent-facing arguments combine OpenAPI locations into one JSON object.
Required, closed object bodies with only property-level constraints are flattened:


```json
{
  "todo_id": 42,
  "verbose": true,
  "title": "Ship SDK docs"
}
```

The compiled `inputBindings` restore each value to its path, query, header, or
body location. Fixed configured headers are applied after rendered input
headers, so credentials cannot be overridden by action arguments.

The browser `performAction` bridge additionally accepts the component envelope
used by Tangram UI code:

```javascript
window.tangram.performAction(
  { resourceType: "Todo", action: "Create" },
  { requestBody: { title: "Ship SDK docs" } }
)
```

The local UI bridge projects `parameters` and `requestBody` using the compiled
bindings before entering the normal host pipeline, preserving whole bodies and
parameter/body name collisions. If every supplied key is already an exposed
argument name, direct arguments take precedence over envelope interpretation
(including arguments literally named `parameters` or `requestBody`).

Optional bodies, open objects (including the OpenAPI default when
`additionalProperties` is omitted), and bodies with composition or other
object-level constraints use a whole `body` argument instead:

```json
{"todo_id": 42, "body": {"title": "Ship SDK docs", "extra": true}}
```

Omitting `body` omits an optional HTTP body; `"body": {}` sends an explicit
empty object, and `"body": null` sends JSON null if the schema permits it.
The exact exposed name is recorded in `inputBindings` and may be prefixed on
collision. Inspect the generated input schema rather than assuming a flat
shape. Recompiling packages with these body schemas changes their argument
shape; update direct callers and regenerate portable skills. Existing compiled
snapshots are not rewritten and must be rebuilt to receive these fixes.

### HTTP parameter serialization

Compiled non-body `inputBindings` record `style` and `explode`. Query parameters
support `form`; path and header parameters support `simple`. Scalars and scalar
arrays are supported. Query arrays with `explode: true` (the default) use repeated
keys (`ids=a&ids=b`); `explode: false` uses a single comma-separated value
(`ids=a,b`). Simple path/header arrays use commas with either explode value.
URL values are escaped individually, keeping embedded commas separate from
serialization delimiters. These defaults follow the
[OpenAPI parameter contract](https://spec.openapis.org/oas/v3.0.3.html#parameter-object).

Other styles, `allowReserved: true`, and declared object/nested-array parameters
fail compilation with a diagnostic. Unconstrained schemas are still checked for
renderable scalar values at invocation. Legacy bindings without serialization
metadata retain their defaults; rebuild snapshots to preserve explicit source
settings, and use an updated SDK to execute graphs containing these fields.

### Supported JSON Schema subset

Runtime validation supports object, array, string, boolean, integer, number,
and null types plus the keywords emitted by the graph compiler, including:

- `properties`, `required`, and `additionalProperties`;
- `items`, `minItems`, and `maxItems`;
- `enum`, `const`, `allOf`, `anyOf`, and `oneOf`;
- `minLength`, `maxLength`, and `pattern`; and
- numeric bounds and `multipleOf`.

The compiler accepts the supported subsets of OpenAPI 3.0 and 3.1 and emits
numeric exclusive bounds and null type unions. OpenAPI 3.0 boolean exclusive
bounds are converted using the corresponding minimum/maximum; `nullable` only
adds null to an explicitly declared type and does not bypass enum or composition
constraints. Schema `$ref` siblings are ignored in 3.0 and applied conjunctively
in 3.1. Reference resolution outside Schema Objects is unchanged. Unsupported
dialects fail compilation; recursive references
and boolean schemas remain unsupported. Enum/const equality distinguishes JSON
booleans from numbers, including within arrays and objects.

## Authorization and confirmation

The default `LocalDevelopmentPolicy` allows Stateless actions only when their
compiled `requires_confirmation` flag is false. It denies mutations unless
their action or binding ID is explicitly allowed. Any allowed action whose
compiled flag is true still returns `CONFIRMATION_REQUIRED` unless it is also
preauthorized.

Grant specific action or binding IDs explicitly:

```python
from tangram_app import LocalDevelopmentPolicy

policy = LocalDevelopmentPolicy(
    allow_mutations={
        "com.example/todos#Todo.Create",
        "com.example/todos#Todo.Toggle",
    },
    preauthorized_confirmations={
        "com.example/todos#Todo.Delete",
    },
)
```

A confirmation-gated mutation must appear in both `allow_mutations` and
`preauthorized_confirmations`. Preauthorization is startup configuration; it
is not evidence that an agent protocol collected per-call human approval.

The local browser UI follows a separate direct-user rule matching Tangram
Desktop:

- actions whose compiled `requires_confirmation` flag is false can run from
  the rendered UI;
- irreversible actions and actions whose compiled flag is true first return
  `CONFIRMATION_REQUIRED`; the compiler defaults the flag to true for
  Reversible effects and write privileges unless the manifest overrides it;
- the browser asks the user; and
- on acceptance it retries the exact frozen request.

Programmatic agent calls remain governed by the configured host policy and do
not inherit the browser's direct-user grant.

Custom policies implement the asynchronous `AuthorizationPolicy.authorize()`
protocol and return `PolicyDecision` with `ALLOW`, `DENY`, or
`CONFIRMATION_REQUIRED`.

## Audit

`JsonlAuditSink` is a local diagnostic record, not a tamper-proof platform
audit log. Events contain metadata only:

- timestamp;
- package digest;
- principal ID and kind;
- action and binding IDs;
- declared effect;
- authorization decision;
- outcome;
- SHA-256 hash of arguments; and
- exception type, when applicable.

Arguments and results are never written to the audit event.

Use `MemoryAuditSink` in tests or implement the `AuditSink` protocol for an
embedded host.

## Generated agent skills

Generate a portable skill snapshot:

```sh
python -m tangram_app skill generate . \
  --output dist/agent-skills/todos \
  --name todos
```

Generation refuses to overwrite an existing output directory. The artifact
contains:

```text
todos/
├── SKILL.md
├── agents/openai.yaml
├── scripts/tangram_agent.py
├── references/capability-graph.json
├── references/tools.md
└── skill.lock.json
```

The lock records graph format, graph SHA-256, and package digest. The runner
verifies all three before every inspection or call.

Inspect and call through a generated skill:

```sh
python /absolute/path/to/todos/scripts/tangram_agent.py inspect

printf '%s' '{}' | python /absolute/path/to/todos/scripts/tangram_agent.py call \
  'com.example/todos#Todo.List@listTodos' \
  --local-package /absolute/path/to/my-app
```

With `--local-package`, the runner recompiles source and refuses if its digest
no longer matches the generated skill. With `--backend`, it calls an already
running loopback backend from the locked graph snapshot.

Generated skills are guidance and discovery artifacts, not authorization
boundaries. Host policy remains authoritative.

## Lower-level embedding API

Use `TangramHost` directly to provide another execution driver, principal,
policy, or audit implementation:

```python
import asyncio

from tangram_app import (
    CapabilityGraph,
    InMemoryDriver,
    LocalDevelopmentPolicy,
    TangramHost,
)

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

print(asyncio.run(host.call("com.example/orders#Order.List", {})))
```

An execution driver implements:

```python
class ExecutionDriver:
    async def invoke(self, action, binding, arguments): ...
```

An audit sink implements:

```python
class AuditSink:
    async def record(self, event): ...
```
