# Command-line reference

[Back to the SDK guide](sdk-guide.md)

## Global install scope

`app install SOURCE --global` selects the current Tangram OS instance's shared
deployment scope (`tangram_os_shared`). It requires an OS instance, ignores
the ambient workspace, and conflicts with an explicit `--workspace`.
Use `--instance NAME` or `--os-url URL --token TOKEN` to select the instance
explicitly. Otherwise the existing ambient-target consent gate applies:
interactive confirmation or `--yes` for non-interactive execution.

The server's existing shared-app policy still applies: only allowlisted
system apps and eligible catalog connectors can deploy globally. OAuth
connectors must support per-user connections. Local workspace-app packages
are rejected, including on `--dry-run`; this flag does not enable arbitrary
apps globally. To install an eligible catalog connector, use the native CLI:

```sh
tangram use prod
tangram app install com.example connector --version 1.0.0 --global
# App Hub plan, followed by the same command without --dry-run to apply:
tangram app pkg hub-install com.example connector 1.0.0 --global --dry-run
```

Global SDK responses include `scope: "global"`, the resolved `instance`,
`target`, and internal `workspace`. A workspace-specific deployment takes
precedence over the shared deployment at runtime.

## Output contract

This page is the reference for the versioned `tangram-app` command surface and
its machine-readable envelopes.

Output modes: when stdout is NOT a tty (agents, scripts, pipes, CI) every
command emits exactly one JSON envelope — the machine contract, unchanged.
On an interactive terminal the same data renders as human-readable text;
pass `--json` to force the envelope there too. Errors follow the same
rule (envelope on stdout when piped; plain text on stderr for humans).

## Command-line interface

`tangram-app --version` emits the version-negotiation envelope
(`{"data":{"version":…,"protocol":"1"}}`).

Both `tangram-app` and `python -m tangram_app` use the same implementation.

The package also installs **`tangram`** — the unified front door: every
command on this page works as `tangram <command>` too (run in-process),
while platform/OS commands (`use`, `login`, `workspace …`, and any `app`
invocation carrying a workspace/instance selector) are forwarded to the
native Tangram CLI when it is on PATH, with an install pointer otherwise.
Translations match the native front door: `app ls` → `app list`,
`app open` → `open`.

| Command | Purpose |
|---|---|
| `doctor [--fix]` | Diagnose prerequisites (Python, Pkl, PostgreSQL, Node) with hints; `--fix` auto-installs the Pkl CLI into `~/.tangram/bin` |
| `validate PACKAGE` | Return structured validation findings |
| `build PACKAGE [--output FILE]` | Compile and write a capability graph |
| `inspect TARGET [--tools | --action ID]` | Inspect a package or graph |
| `run PACKAGE` | Run backend/database/UI until interrupted |
| `open TARGET [--no-browser]` | Run like `run`, then open the app UI in the browser |
| `app install SOURCE [--upgrade]` | Validate + install a package (dir, tar.gz/zip, https URL) into `~/.tangram/apps/`; `--upgrade` replaces an existing install (`--force` is a deprecated alias) |
| `app install SOURCE --workspace [I/]WS [--instance N \| --os-url URL --token T] [--dry-run] [--upgrade] [--yes]` | Deploy into a Tangram OS workspace (native credentials reused; qualified `INSTANCE/WS` is the canonical address; ambient context needs a TTY confirmation or `--yes`; conflicting selectors refuse) |
| `app list` | List installed apps (normalized rows: id, version, context, state, detail) |
| `app get REF` | One installed app's normalized details |
| `app uninstall REF [--yes]` | Remove an installed app + its `.preview` state (TTY confirms; non-interactive requires `--yes`) |
| `actions TARGET` | Compact action catalog (short refs, effects, bindings) |
| `call TARGET REF --local [--allow-mutation] [--confirm]` | Invoke once (`REF` = `Action`, `ResourceType.Action`, or full id); attaches to a live `run`/`open` session when one exists, else boots and stops; flags grant that one action's mutation/confirmation |
| `call TARGET ID --backend URL` | Invoke an already-running loopback backend |
| `connect TARGET --oauth [--client-id ID --client-secret S] [--no-browser]` | Run the connector's real OAuth dance with a developer-registered client (loopback callback, PKCE, tenant capture; auto-refresh on later calls) |
| `connect TARGET --token T [--tenant X]` | Store a developer OAuth token for a connector (`--token -` reads stdin) |
| `call TARGET ID --connected [--endpoint URL]` | Execute a connector action against its vendor endpoint with the stored token |
| `disconnect TARGET` | Remove the stored developer connection |
| `skill generate TARGET --output DIR` | Generate an integrity-locked agent skill |
| `skill install` | Install EVERY bundled authoring skill for both Claude Code (`~/.claude/skills/`) and Codex (`~/.codex/prompts/` + AGENTS.md pointer) — idempotent machine setup |
| `skill install [NAME] [--project DIR \| --user \| --codex] [--force]` | Install one bundled skill (or all, when NAME is omitted) into a single scope; a NAME without a scope defaults to `--project .` |
| `skill install-builder [--project DIR \| --user \| --codex] [--force]` | Alias for `skill install tangram-app-builder` |
| `skill sync` | Project installed apps as managed agent skills (Claude Code + Codex catalogs); reconciles add/update/remove per active context, reports conflicts. ON by default — `app install` runs it automatically (`--no-skills` opts out) |
| `skill sync --enable \| --disable \| --status \| --approve DIGEST \| --rebuild` | Re-enable / turn off projection (disable removes managed skills), read-only status, approve an app's free-form text by digest, backfill projection records for older installs |
| `call \| inspect … --package-digest sha256:…` | Refuse when the installed package no longer matches the digest a generated skill was rendered from |
| `actions APP --workspace WS [--instance N \| --os-url U --token T]` | A workspace app's declared action contracts (effect, confirmation, schemas) from the OS entity-action surface, with the native CLI's stored credentials |
| `call APP REF --workspace WS --input-json '{…}' [--allow-mutation]` | Perform one workspace app action as you, server-governed (per-type IAM, audited); short refs resolve against the server catalog; confirmation-gated and irreversible actions refuse (use the app UI) |
| `call APP REF --input-json '{…}'` (bare) | Context decides: with an ambient workspace (`tangram use I/WS`) this is the OS lane against it; in the machine context it means `--local`. `--workspace` given bare selects the ambient one explicitly; explicit flags always win |
| `TANGRAM_INSTANCE` / `TANGRAM_WORKSPACE` env | Pin the ambient context for one shell or agent session without touching the shared `.HEAD` (precedence: flags > env > `.HEAD` > machine). An env instance never pairs with the `.HEAD` workspace |

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

### Simple calls in the active context

```sh
tangram app search 'orders'
tangram app inspect 'com.example/orders#Order.List'
tangram app call 'com.example/orders#Order.List' --input '{}'
```

`--target-ref`, `--contract`, and `--invocation-id` are optional. The CLI
resolves the active context once, resolves the exact action binding and current
contract, and generates an invocation UUID. `--input` aliases `--input-json`;
omitting both reads JSON from stdin. Ambiguous actions refuse. Explicit pins
retain their existing meaning.

Use `--local` or `--workspace INSTANCE/WORKSPACE` to override active context.
Selectors cannot be combined with `--target-ref`. Local gated mutations use the
desktop approval popup. Remote mutations require an explicit target and retain
the server's policy and approval behavior; this does not add remote desktop
approval. Results include the resolved target.

Before dispatch, the CLI saves the frozen target reference, action, contract,
input, and invocation ID in the owner-only `~/.tangram/hub/invocations.sqlite3`
database (under `TANGRAM_HOME` when overridden). Inputs are retained for explicit
resume; this receipt is not an approval grant. Results and uncertain failures
include `invocationId` and `resumeCommand`:

```sh
tangram app invocation <invocationId>  # status, using the saved target
tangram app resume <invocationId>      # same target, input, contract and UUID
```

Resume does not follow a changed active context or refresh a changed contract.
The governing journal still prevents replay of running or completed executions.
A missing response can mean the action committed; reconcile app state when the
result is `unknown`. Repeating the original simple command creates a **new
invocation**, so use the returned resume command after uncertainty. Older
invocations without a client receipt still support `app invocation --target-ref
...`; they cannot use `app resume`.
