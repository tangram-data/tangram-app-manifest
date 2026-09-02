# Command-line reference

[Back to the SDK guide](sdk-guide.md)

This page is the reference for the versioned `tangram-app` command surface and
its machine-readable envelopes.

## Command-line interface

Both `tangram-app` and `python -m tangram_app` use the same implementation.

| Command | Purpose |
|---|---|
| `doctor [--fix]` | Diagnose prerequisites (Python, Pkl, PostgreSQL, Node) with hints; `--fix` auto-installs the Pkl CLI into `~/.tangram/bin` |
| `validate PACKAGE` | Return structured validation findings |
| `build PACKAGE [--output FILE]` | Compile and write a capability graph |
| `inspect TARGET [--tools | --action ID]` | Inspect a package or graph |
| `run PACKAGE` | Run backend/database/UI until interrupted |
| `open TARGET [--no-browser]` | Run like `run`, then open the app UI in the browser |
| `app install SOURCE [--force]` | Validate + install a package (dir, tar.gz/zip, https URL) into `~/.tangram/apps/` |
| `app install SOURCE --workspace WS [--instance N \| --os-url URL --token T] [--dry-run] [--upgrade]` | Deploy the package into a Tangram OS workspace (native-CLI credentials reused) |
| `app list` | List installed apps |
| `app uninstall REF` | Remove an installed app |
| `actions TARGET` | Compact action catalog (short refs, effects, bindings) |
| `call TARGET REF --local [--allow-mutation] [--confirm]` | Invoke once (`REF` = `Action`, `ResourceType.Action`, or full id); attaches to a live `run`/`open` session when one exists, else boots and stops; flags grant that one action's mutation/confirmation |
| `call TARGET ID --backend URL` | Invoke an already-running loopback backend |
| `connect TARGET --oauth [--client-id ID --client-secret S] [--no-browser]` | Run the connector's real OAuth dance with a developer-registered client (loopback callback, PKCE, tenant capture; auto-refresh on later calls) |
| `connect TARGET --token T [--tenant X]` | Store a developer OAuth token for a connector (`--token -` reads stdin) |
| `call TARGET ID --connected [--endpoint URL]` | Execute a connector action against its vendor endpoint with the stored token |
| `disconnect TARGET` | Remove the stored developer connection |
| `skill generate TARGET --output DIR` | Generate an integrity-locked agent skill |
| `skill install-builder [--project DIR \| --user] [--force]` | Install the bundled app-authoring skill into `.claude/skills/` |

`TARGET` is a package directory, a capability graph JSON file, or an
installed app reference — the app id (`com.example/orders`) or its bare
name when unique. Installed apps live under `~/.tangram/apps/` (override
with `TANGRAM_HOME`); `install` validates the package before copying and
records id/version/source in `.install.json`. `--local` requires a source
package directory (installed apps qualify).

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
