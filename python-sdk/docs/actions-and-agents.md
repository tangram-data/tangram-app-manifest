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

Agent-facing arguments are a flat JSON object even when OpenAPI uses several
locations:

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

The local UI bridge flattens `parameters` and `requestBody` before entering the
normal host pipeline.

### Supported JSON Schema subset

Runtime validation supports object, array, string, boolean, integer, number,
and null types plus the keywords emitted by the graph compiler, including:

- `properties`, `required`, and `additionalProperties`;
- `items`, `minItems`, and `maxItems`;
- `enum`, `const`, `allOf`, `anyOf`, and `oneOf`;
- `minLength`, `maxLength`, and `pattern`; and
- numeric bounds and `multipleOf`.

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
