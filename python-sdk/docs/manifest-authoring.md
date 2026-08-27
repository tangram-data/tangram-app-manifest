# Manifest authoring and compilation

[Back to the SDK guide](sdk-guide.md)

This page covers the source-package API, manifest models, validation, and
capability graph compilation.

## `TangramProject`

`TangramProject` represents a mutable canonical source package.

```python
from tangram_app import TangramProject

project = TangramProject.open("/absolute/path/to/my-app")
```

`TangramProject.open(path)` resolves the path and requires a `manifests/`
directory. It does not validate the package immediately.

Available operations:

| Method | Result | Purpose |
|---|---|---|
| `validate()` | `ValidationResult` | Load and validate source without Tangram OS |
| `compile()` | `TangramApp` | Validate and compile an executable capability graph |
| `backend_spec()` | `BackendSpec` | Read the canonical Python backend declaration |
| `run_local(...)` | `LocalAppSession` | Start the package's local runtime |

Example:

```python
import asyncio

from tangram_app import TangramProject

project = TangramProject.open(".")
validation = project.validate()
validation.require_valid()

with project.run_local() as running:
    print(running.backend_url)
    print(running.ui_url)
    todos = asyncio.run(
        running.call("com.example/todos#Todo.List@listTodos", {})
    )
    print(todos)
```

## Validation and manifest models

Use `validate_manifest()` when normal manifest problems should be returned as
structured findings rather than exceptions:

```python
from tangram_app import Severity, validate_manifest

result = validate_manifest(".")

for finding in result.findings:
    print(finding.severity.value, finding.code, finding.path, finding.message)

if result.valid:
    package = result.require_valid()
else:
    for error in result.errors:
        print(error.message)
```

`ValidationResult` provides `findings`, `errors`, `warnings`, `valid`, and
`require_valid()`. `require_valid()` returns `ManifestPackage` or raises
`ManifestValidationError`.

Validation includes:

- required package layout and removed legacy paths;
- dependency-lock shape and checksums;
- restricted Pkl evaluation;
- application identity and type-specific constraints;
- configuration, resource type, version, action, role, and privilege rules;
- OpenAPI 3 parsing and operation ID uniqueness;
- OpenAPI mapping and extractor references;
- Connector endpoint and OAuth checks;
- Agent tool/skill shapes; and
- root UI component and source path checks.

Deployment schema reflection and some integration modules currently produce
explicit coverage warnings. Use the Tangram manifest CLI/Scala validator as
the authoritative publishing check.

For direct model access:

```python
from tangram_app import PklManifestLoader

package = PklManifestLoader().load(".")
application = package.application
todo = package.resource_type("Todo")
create = todo.active.action("Create")
```

The Python dataclasses mirror the public manifest-facing Scala vocabulary,
including `Application`, `AppResourceTypeDefinition`,
`AppResourceTypeVersion`, `ResourceTypeAction`, `OpenApiMapping`, roles,
privileges, extractors, settings, and secrets.

## Capability graph compilation

Compile source with:

```python
from tangram_app import compile_manifest

compiled = compile_manifest(".")
compiled.graph.write_file("dist/tangram-app.json")
```

Compilation:

1. requires a valid package;
2. selects each resource type's active version;
3. includes actions with `openApiMapping` or `openApiMappings`;
4. resolves each mapping to an OpenAPI operation;
5. flattens path, query, header, and JSON-body inputs into one agent schema;
6. stores reverse input bindings for HTTP request rendering;
7. selects the supported success response schema;
8. records runtime requirements and root UI metadata; and
9. calculates a deterministic package digest over `manifests/` and `libs/`.

The graph wire schema is published at
`python-sdk/schema/capability-graph.schema.json`.

Local Pkl project dependencies set `developmentOnly: true`. Authority and
development-only status survive graph serialization and cannot be upgraded by
loading the snapshot later.
