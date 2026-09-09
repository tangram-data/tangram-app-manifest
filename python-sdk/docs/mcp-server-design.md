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

| Tool | Result (the CLI's `data` payload, verbatim) | Mirrors |
|---|---|---|
| `tangram_list_apps` | `{apps: [{id, version, context, state, detail}, …]}` | `app list` |
| `tangram_app_actions {app}` | `{app, actions: [{ref, id, effect, requiresConfirmation, doc, bindings}, …]}` | `actions <app>` |
| `tangram_call_action {app, action, args?, allow_mutation?}` | `{bindingId, result}` (+ `session: "attached"` when a warm session served it) | `call <app> <ref> --local --input-json … [--allow-mutation]` |

**Decision — results are the CLI's `data` payloads EXACTLY** (the
shapes above are today's actual envelopes; no MCP-side enrichment like
titles or parameter summaries in v1). One contract, one set of
conformance vectors; hosts wanting parameter schemas fetch the action's
declared schema via a later enrichment, not a diverging v1 shape.

Every tool result — success or failure — carries BOTH the payload as
`structuredContent` AND a `content` text block holding the same payload
serialized (the MCP schema requires `content` unconditionally).

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
  refs, ambiguity → `invalid_arguments` naming the candidates), per-call
  policy scoped to the one resolved action, warm-session attach.
- **Mutations default-deny.** `allow_mutation: true` mirrors
  `--allow-mutation` — the same trust model as the CLI flag: an explicit
  consent token recorded in the transcript, not proof of a human.
- **Confirmation-gated actions are DENIED over MCP in v1.** There is no
  `confirm` param: a model-supplied boolean is not human approval, and
  MCP annotations are advisory — they cannot force a host prompt. The
  error tells the caller to use the CLI (`--confirm`) or the app UI;
  revisit when MCP elicitation gives a trusted host-mediated approval.
- **MCP tool annotations** carry the safety surface to hosts that honor
  them: `readOnlyHint: true` on the two listing tools;
  `tangram_call_action` annotated non-read-only + `destructiveHint`.
  Annotations are UX, never enforcement.
- **Errors**: CLI error envelopes today are `{code, message}` (no
  `retryable` at this boundary). MCP failures MUST be a
  `CallToolResult` with `isError: true`, a `content` text block (the
  serialized error envelope — `content` is required by the MCP schema
  even when `structuredContent` is present), and the envelope again as
  `structuredContent`. A conforming failure path is an **M1**
  requirement, not deferred polish.
- **Audit is real, not implied**: `bind()` without an `audit_path`
  installs a null sink, so the server always supplies a server-owned
  destination for every `tangram_call_action`. One JSONL file **per
  server process** (`<TANGRAM_HOME>/audit/mcp-<pid>-<start>.jsonl`) —
  the existing `JsonlAuditSink` writes record and newline separately
  with no inter-process locking, so a shared file across concurrent
  hosts could interleave; per-process files avoid that without new
  locking machinery.

## 6. Backend lifecycle

- **Attach-if-warm**: same rules as the CLI (`.preview/session.json`,
  pid alive, loopback scheme, `/openapi.json` probe) — ~0.4s per call
  against a warm session.
- **Cold path**: the server is long-lived (spawned by the host), so on
  first call to a cold app it starts the managed backend, keeps it
  warm for subsequent calls, and writes the same `session.json` so the
  CLI shares the session. All owned sessions stop on server shutdown.
- **Session identity tracks the install**: an owned session is keyed to
  the installation it was started from (package digest / `.install.json`
  identity). Upgrade, uninstall, or reinstall of the app invalidates the
  owned session — it is stopped and the next call cold-starts the new
  code. Without this, "dynamic data" would silently serve a stale graph.
- **Cold-start contention is real**: today the runtime takes an
  exclusive non-blocking project lock and `session.json` is written
  non-atomically, so two concurrent cold callers race — the loser gets
  `local_runtime_failed` rather than attaching. M3 must add
  retry-then-reattach on lock failure and atomic session-marker writes
  before the server may own sessions.
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
  for its test server.
- **Lifecycle per the MCP spec, not ad hoc**: the supported
  protocol-version set for v1 is exactly `{"2025-06-18"}`. When a
  client requests a version outside the set the server RESPONDS with
  `2025-06-18` (the client decides whether to disconnect) — it does
  not reject. The server waits for `notifications/initialized` before
  serving tool traffic. Widening the set is a deliberate,
  conformance-tested change.
- **Explicit size caps** (the CLI lane has none on argument input, so
  the server enforces its own): maximum inbound JSON-RPC message and
  `args` object 1 MiB (matching the local-actions bridge cap), maximum
  serialized tool result 4 MiB with a structured `result_too_large`
  error; enforced before parsing/serializing.
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
  approval prompts via annotations on top. Confirmation-gated actions
  denied outright (§5).
- No secrets in any tool result; connector connections are out of
  scope v1.
- Server-enforced size caps (§7); schema validation identical to the
  CLI lane; nothing from tool input reaches a shell.
- Concurrent hosts race on cold starts today (exclusive project lock +
  non-atomic `session.json` — see §6); until M3 lands the
  retry/atomic-marker work, concurrent cold callers can observe
  `local_runtime_failed` and simply retry.

## 9. Milestones

- **M1** — `local_mcp.py` (lifecycle per spec, tools/list, tools/call),
  the three tools with `tangram_call_action` read-only-only
  (`allow_mutation` refused with `policy_denied`), the conforming
  result contract for success AND failure (`content` block +
  `structuredContent`, `isError: true` on failures), size caps,
  server-owned audit file, `mcp` verb in the parser, unit tests driving
  a scripted JSON-RPC session against a store fixture.
- **M2** — `allow_mutation` + annotations; conformance vectors for the
  tool envelopes; confirmation-gated denial message.
- **M3** — warm-session ownership (start/keep/stop, shared
  session.json, install-identity eviction) on top of runtime fixes:
  retry-then-reattach on the project lock, atomic session-marker
  writes; latency docs.
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
   (e.g. `surface: mcp`) inside the server-owned audit records, or
   stay field-identical to CLI audit records?
2. The exact concurrency rule for M3 session ownership (retry budget,
   who wins the lock, marker format) — decided alongside the runtime
   atomic-write fix, not before.
3. Whether `tangram_call_action` should accept `args` as a JSON object
   only, or also the CLI's `key=value` sugar (leaning: object only —
   MCP hosts are structured callers).
4. Whether/when MCP elicitation is mature enough across target hosts to
   lift the confirmation-gated denial.
