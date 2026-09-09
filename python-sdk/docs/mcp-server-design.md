# Tangram Apps MCP Server — Design

Status: DRAFT for review — no implementation yet.

## 1. Problem

Agents on tool-first or shell-less surfaces (Codex desktop, Claude
desktop, the Tangram desktop app's own agent panel) do not discover the
machine's installed Tangram apps or the governed action lane. Observed
2026-09-09 with Codex: asked to "list all the todos", it inspected the
project working tree and answered "the app's database hasn't been
created" (wrong — the backend held data); asked to "call app action
list todo", it found a listening port with `ps`/`lsof` and curled the
backend directly, bypassing schema validation, policy, and audit.

Prose guidance (AGENTS.md blocks, skills) only helps agents that load
it. An MCP server makes the governed lane a first-class *tool surface*:
the shortest path becomes the correct path.

## 2. Goals

- **Machine context only**: the `~/.tangram` app store + local runtime,
  exactly what `tangram-app app list` / `actions` / `call --local` see.
- **Dynamic**: every response reflects the store at call time; installs
  and uninstalls need no server restart.
- **Governance parity**: tool calls run the *same* pipeline as the CLI
  (target resolution incl. short refs, schema validation,
  LocalDevelopmentPolicy gates, audit, warm-session attach). No
  parallel lane.
- **Tiny context footprint**: a fixed handful of tools regardless of
  how many apps/actions are installed.

## 3. Non-goals (v1)

- Workspace / Tangram OS lanes (`--workspace`, instances) — the OS has
  its own agent surfaces.
- Connector developer connections (`call --connected`) — excluded until
  the secrets story for a long-lived server is designed.
- Authoring verbs (validate/build/inspect/skill) — agents with a shell
  already have the CLI; authoring needs file access anyway.
- Replacing the CLI or the skills; the MCP server is an additive
  projection of the same lane.

## 4. Shape: one server, three tools

New verb `tangram-app mcp`: a **stdio** MCP server (new module
`local_mcp.py`, within the new-file LOC budget). Tools:

| Tool | Result | Mirrors |
|---|---|---|
| `tangram_list_apps` | installed apps: id, version, state, description | `app list` |
| `tangram_app_actions {app}` | compact catalog: short ref, title, effect, requiresConfirmation, params summary | `actions <app>` |
| `tangram_call_action {app, action, args?, allow_mutation?, confirm?}` | one governed invocation's result | `call <app> <ref> --local [--allow-mutation] [--confirm]` |

**Decision — static tool surface, dynamic data.** NOT one MCP tool per
installed action. Rationale: context economics (N apps × M actions
would bloat every agent turn and churn on every install), and it keeps
one teaching across CLI, skills, and MCP. Per-action tool projection
(typed input schemas per tool + `notifications/tools/list_changed`) is
a possible later experiment, not v1; v1 simply re-reads the store on
every call — cheap and always fresh, no watcher, no cache.

Tool results are the CLI's agent-lane JSON envelopes verbatim (the
`data` payloads), so anything already written against the CLI contract
transfers.

## 5. Governance and mutation consent

- `tangram_call_action` binds through the identical code path as
  `call --local`: resolver (id or unique bare name, `RT.Action` short
  refs, ambiguity → candidate list), per-call policy scoped to the one
  resolved action, audit_path, warm-session attach.
- **Mutations default-deny.** `allow_mutation: true` mirrors
  `--allow-mutation`; `confirm: true` mirrors `--confirm` for
  confirmation-gated actions. The server never escalates on its own.
- **MCP tool annotations** carry the safety surface to the host UI:
  `readOnlyHint: true` on the two listing tools;
  `tangram_call_action` is annotated non-read-only (and
  `destructiveHint`) so hosts that gate tool use prompt the human.
- Errors use the SDK's structured contract `{code, message,
  retryable}` — `policy_denied` for a mutation without
  `allow_mutation`, `invalid_arguments` with candidates on ambiguity,
  etc.

## 6. Backend lifecycle

- **Attach-if-warm**: same rules as the CLI (`.preview/session.json`,
  pid alive, loopback scheme, `/openapi.json` probe) — ~0.4s per call
  against a warm session.
- **Cold path**: the server is long-lived (spawned by the host), so on
  first call to a cold app it starts the managed backend, keeps it
  warm for subsequent calls, and writes the same `session.json` so the
  CLI shares the session. All owned sessions stop on server shutdown.
- Latency is documented honestly: first cold call pays the ~30s
  managed-environment start; hosts wanting instant first calls can run
  `tangram-app open <app>` beforehand.

## 7. Protocol and packaging

- **stdio only.** No TCP listener: the server is a child of the host,
  inherits user identity, exposes nothing on the network.
- **Dependency-free implementation.** The SDK deliberately has one
  runtime dependency (PyYAML); the official `mcp` package brings a
  tree. MCP's server core here is small (initialize, `tools/list`,
  `tools/call`, ping, notifications), so `local_mcp.py` implements the
  JSON-RPC framing directly — the same choice the desktop repo made
  for its test server. Supported protocol version is pinned and
  echoed at initialize; unknown client versions get a clean error.
- Server info reports the SDK `__version__`; the §15 capability tier
  table gains an `mcp` row when this ships.
- **Host recipes** (documented in cli-reference + skills):
  - Claude Code: `claude mcp add tangram -- tangram-app mcp`
  - Codex: `[mcp_servers.tangram] command = "tangram-app", args = ["mcp"]`
  - Tangram desktop: its MCP client config (`<stateDir>/mcp-servers.json`)
    pointing at the **bundled** SDK launcher (`…/sdk/bin/tangram-app mcp`)
    — the Phase-3 bundle means the desktop can self-register this with
    zero user setup. That lands in the tangram-desktop repo.
- **Front door**: `mcp` joins the native CLI's passthrough set, so
  `tangram mcp` works wherever the desktop installed the wrapper
  (tangram repo, additive).

## 8. Security notes

- Mutation double gate: explicit tool param AND per-action policy; host
  approval prompts via annotations on top.
- No secrets in any tool result; connector connections are out of
  scope v1.
- Args size caps and validation identical to the CLI lane; nothing
  from tool input reaches a shell.
- Concurrent hosts: two servers may own different sessions; session
  files are written atomically and attach checks probe liveness, so
  the worst case is a redundant backend, not corruption. (Open
  question below.)

## 9. Milestones

- **M1** — `local_mcp.py` (initialize/tools/list/tools/call), the three
  tools with `tangram_call_action` read-only-only (mutation params
  refused with `policy_denied`), `mcp` verb in the parser, unit tests
  driving a scripted JSON-RPC session against a store fixture.
- **M2** — mutation params + annotations + structured error mapping;
  conformance vectors for the tool envelopes.
- **M3** — warm-session ownership (start/keep/stop, shared
  session.json) + latency docs.
- **M4** — host recipes in docs/skills; desktop self-registration
  (tangram-desktop repo); `mcp` passthrough (tangram repo).
- **M5 (maybe)** — `tools/list_changed` + per-action tool projection
  experiment, only with evidence static tools are limiting hosts.

## 10. Rejected alternatives

- **One MCP tool per installed action** — context bloat scaling with
  the store, tool-list churn on install/uninstall, and a second place
  where action schemas must stay honest. Progressive disclosure via
  `tangram_app_actions` matches the platform's existing tool-catalog
  philosophy.
- **HTTP/SSE transport wrapping LocalActionsServer** — an extra open
  port and bearer-token handshake for no host we target; every current
  host speaks stdio.
- **Implementing the server in the native Scala CLI** — the store,
  resolver, policy, and session machinery all live in the python-sdk;
  a Scala server would duplicate the lane the front door exists to
  avoid duplicating.
- **Desktop-only MCP server in tangram-desktop** — would not serve
  Claude Code / Codex CLI users; the SDK is the shared home, and the
  desktop simply registers the bundled copy.

## 11. Open questions

1. Should MCP-originated calls carry a distinct audit surface tag
   (e.g. `surface: mcp`) vs staying byte-identical to CLI audit v1?
2. Session ownership when several hosts run servers concurrently —
   first-writer-wins on `session.json` plus liveness probing may be
   enough; needs a decided rule before M3.
3. Whether `tangram_call_action` should accept `args` as a JSON object
   only, or also the CLI's `key=value` sugar (leaning: object only —
   MCP hosts are structured callers).
