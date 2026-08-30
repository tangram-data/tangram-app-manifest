# Command-line reference

[Back to the SDK guide](sdk-guide.md)

This page is the reference for the versioned `tangram-app` command surface and
its machine-readable envelopes.

## Command-line interface

Both `tangram-app` and `python -m tangram_app` use the same implementation.

| Command | Purpose |
|---|---|
| `validate PACKAGE` | Return structured validation findings |
| `build PACKAGE [--output FILE]` | Compile and write a capability graph |
| `inspect TARGET [--tools | --action ID]` | Inspect a package or graph |
| `run PACKAGE` | Run backend/database/UI until interrupted |
| `call TARGET ID --local` | Start source, invoke once, stop |
| `call TARGET ID --backend URL` | Invoke an already-running loopback backend |
| `skill generate TARGET --output DIR` | Generate an integrity-locked agent skill |
| `skill install-builder [--project DIR \| --user] [--force]` | Install the bundled app-authoring skill into `.claude/skills/` |

`TARGET` is a package directory or a capability graph JSON file. `--local`
requires a source package directory.

Action input is a JSON object supplied by `--input-json` or stdin:

```sh
python -m tangram_app call . \
  'com.example/todos#Todo.Create@createTodo' \
  --local \
  --input-json '{"title":"Write docs"}'
```

The CLI default policy is read-only, so this mutating example will return
`policy_denied`. Mutation grants are currently available through the Python
policy API, not CLI flags.

### JSON envelope

Every command writes exactly one compact JSON document to stdout:

```json
{"schemaVersion":"1","ok":true,"data":{}}
```

Errors use:

```json
{"schemaVersion":"1","ok":false,"error":{"code":"invalid_input","message":"..."}}
```

Exit status classes:

| Status | Meaning |
|---:|---|
| `0` | Success or requested foreground shutdown |
| `1` | Internal/unexpected SDK failure |
| `2` | Invalid arguments, graph shape, or manifest source |
| `3` | Valid request that could not execute |

Stable error codes currently include:

- `invalid_arguments`;
- `capability_graph_stale`;
- `unknown_binding`;
- `ambiguous_action`;
- `invalid_input`;
- `confirmation_required`;
- `policy_denied`;
- `unsupported_requirement`;
- `local_runtime_failed`;
- `upstream_failed`;
- `graph_invalid`;
- `manifest_invalid`; and
- `internal_error`.

Agents should branch on `ok` and `error.code`, not parse human messages.
