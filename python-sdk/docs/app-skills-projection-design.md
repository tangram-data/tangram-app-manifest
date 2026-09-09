# Installed Apps as Agent Skills — Design

Status: DRAFT for review — no implementation yet.
Companion to `mcp-server-design.md`: two projections of one governed
lane. Skills serve shell-capable agents (Claude Code, Codex CLI); MCP
serves shell-less hosts.

## 1. Problem

Agents route requests by what is in their context. Skill names and
descriptions are always there (Claude Code loads the catalog each
session; Codex reads `~/.codex/prompts` + the AGENTS.md block), so a
`tangram-todo-list` skill described as "manage todos in the installed
Todo app" matches *"list my todos"* by domain words — routing the
generic usage prose we ship in AGENTS.md cannot provide. Today nothing
projects the store into agent homes: installing an app changes what the
machine can do but not what agents know.

The same gap exists one level up: in the v3 context model the SAME
verbs target a remote Tangram OS workspace after `tangram use <ctx>`.
Apps installed in that workspace should reach agents the same way.

## 2. Goals

- Installing an app makes it *discoverable and usable* by Claude Code
  and Codex with no manual step; uninstalling removes it; upgrading
  refreshes it.
- One governed lane: projected skills teach the `tangram-app`/`tangram`
  CLI (`actions`, `call --local --input-json …`, mutation flags,
  never-curl). Skills are routing + instructions, never a new
  enforcement or execution surface.
- Context-consistent: what is projected follows the v3 context model —
  the machine store always, plus the active instance/workspace context.
- Agent homes stay healthy: we own only what we mark, never clobber
  user-authored skills, and everything is reconciled idempotently.

## 3. Non-goals (v1)

- A file watcher or daemon — projection reconciles at well-defined
  moments (below), not continuously.
- Replacing `skill generate` for app *authors* shipping standalone
  skill bundles; this design projects *installed* apps.
- Per-action skills (one skill per app; actions are progressive
  disclosure inside it).
- Agent platforms beyond Claude Code and Codex (the renderer is
  pluggable; two targets v1).

## 4. Core primitive: `skill sync`, a reconciler

`tangram-app skill sync` computes the DESIRED skill set from the
machine store (`~/.tangram/apps`) plus the active context, compares it
to the MANAGED entries in each agent home, and converges:

- add missing skills, rewrite changed ones, remove managed skills whose
  app is gone;
- never touch anything unmanaged (see ownership below);
- exit report lists added/updated/removed/conflicts.

Lifecycle verbs call it internally: `app install`, `app uninstall`,
`app install --upgrade`, and context switch. `skill sync` run by hand
repairs anything (wiped skills dir, edited files, missed events). This
mirrors the repo's other convergence loops (desktop wrapper
auto-install, AGENTS.md marked block): derived state is always
recomputable from the source of truth.

## 5. What gets projected (thin, not the fat bundle)

`generate_skill` today emits a self-contained bundle (runner script +
a COPY of the capability graph + lock) built for project apps — the
runner re-verifies the graph against the local package. Projected
skills deliberately do NOT reuse that shape: a second graph copy per
agent home drifts, and the runner assumes a project layout. Instead a
projected skill is:

- `SKILL.md` — frontmatter (name, description, managed marker,
  install digest) + body: what the app is, the action catalog (ref,
  effect, requiresConfirmation, doc — rendered from the graph), and
  the exact CLI loop for THIS app (`tangram-app actions <id>`,
  `call <id> <Ref> --local --input-json '{…}'`, mutation flags,
  never-curl). The store remains the single source of truth for
  schemas; the skill tells the agent to ask the CLI, not to trust a
  cached copy.

Frontmatter marker (ownership, mirrored from the wrapper/AGENTS.md
pattern):

```yaml
managed-by: tangram-app-sdk
tangram-app: com.example/todo-list
tangram-context: machine            # or instance/workspace
package-digest: sha256:…
```

Only files carrying `managed-by: tangram-app-sdk` are ever rewritten
or removed. A user-authored skill occupying a managed name is reported
as a conflict and left alone.

Naming: `tangram-<app-name>` for machine context;
`tangram-<workspace>-<app-name>` for workspace apps (both normalized
by the existing `normalize_skill_name`). Description = the app
manifest description plus an origin suffix ("installed on this
machine" / "in workspace <ws>") — the description is the router, so it
must carry the domain words.

## 6. Agent targets

- **Claude Code**: `~/.claude/skills/<name>/SKILL.md` (user scope —
  the store is user-scoped). Project scope is out: the store isn't
  per-project.
- **Codex**: `~/.codex/prompts/<name>.md` (same body) plus per-app
  lines inside the existing marked AGENTS.md block ("Before working
  with <description-derived phrase>, read `~/.codex/prompts/<name>.md`
  first."). The block section for managed apps is capped (see §9) —
  the prompts themselves are invokable regardless.

## 7. Context switching and remote workspace apps

The v3 model: one verb table, destination decided by context
(`machine` default; `tangram use <ctx>` selects an instance/workspace).
Projection follows it:

- **Desired set = machine apps ∪ active-context workspace apps.**
  `tangram use` triggers `skill sync`; switching away removes the
  previous workspace's managed skills and projects the new one's.
- **Availability follows context; invocation is PINNED.** A workspace
  skill's commands embed the explicit selector
  (`--workspace WS --instance N`), never relying on ambient context:
  an agent may hold a session across a context switch, and a
  pinned command still targets what the skill describes — same
  reasoning as pinned digests elsewhere in the platform. (Rejected:
  ambient-following commands; silent retargeting of an agent's calls
  is exactly the surprise the platform avoids.)
- **Catalog source**: workspace app lists already exist on the native
  lane (`app list --workspace`); action catalogs come from the OS's
  app schema introspection, fetched at sync time with the stored
  credentials (`~/.tangram/.credentials`, the lane `local_os.py`
  already reuses).
- **DEPENDENCY — the workspace call lane.** Today `call` is
  `--local`/`--connected`/`--backend` only; there is no
  `call … --workspace` from the CLI, though the governed server-side
  pipeline (policy, confirmation, audit) exists and is what OS agent
  tools use. Projecting workspace apps is gated on that verb landing
  (native CLI or python-sdk over the OS HTTP API — same wire-reuse
  pattern as the `app install --workspace` lane). Confirmation-gated
  actions follow the OS's own rules server-side; the CLI adds no
  consent theater of its own.
- **Staleness**: remote installs change without local events. v1
  reconciles at `use` / `sync` / workspace `app` verbs and documents
  that; no polling. A stale skill fails safe — the CLI errors name
  the missing app/action, and `skill sync` repairs.
- **Credentials**: skills never embed tokens; commands ride the stored
  credential lane, and an expired login surfaces as the CLI's own
  login prompt/error.

## 8. Prompt injection (the risk that needs its own section)

Projected skill text originates in app manifests — third-party content
(hub apps; for workspaces, other members' apps) that would be injected
into EVERY agent session's context automatically. Treat it as
untrusted:

- Render only structured fields (name, description, action refs,
  effects, docs); never pass free-form manifest content through
  verbatim into instruction position.
- Cap lengths (description, per-action doc) and strip/neutralize
  imperative patterns ("ignore previous instructions", tool-call
  syntax, markdown links masquerading as directives) — escaping into a
  quoted "app says:" framing rather than silent dropping.
- The skill body's instructions (the CLI loop) are OURS, templated;
  app content appears only as labeled data within it.
- `app install` prints the skill name + description it registered, so
  a surprising description is visible at consent time.
- Workspace projection inherits workspace trust: installing an app
  into a workspace is already an admin-consented act; the cap and
  labeling still apply.

## 9. Consent and clutter

- First `skill sync` per agent home asks once (or inherits the consent
  of an explicit bare `skill install`, which already writes these
  homes); recorded under `~/.tangram/`. `app install --no-skills`
  opts a single install out; a config key disables projection
  entirely.
- Workspace projection is capped (e.g. 20 apps) with an explicit
  warning listing what was skipped — a large shared workspace must not
  flood the agent catalog. `skill sync --all` lifts the cap by hand.
- Everything managed is enumerable: `skill sync --status` lists
  managed skills, their digests, and conflicts.

## 10. Sequencing vs the MCP server

Skills-first: S1/S2 below are cheaper than MCP M1 (no protocol work),
serve both target agents immediately, and would have fixed the observed
Codex incident outright. The MCP server remains the answer for
shell-less hosts; both projections must stay contract-identical to the
CLI lane so neither drifts.

## 11. Milestones

- **S1** — machine-context `skill sync` + Claude Code target: managed
  SKILL.md rendering (with the §8 sanitizer), reconciler with
  ownership rules, `--status`, unit tests over a store fixture.
- **S2** — Codex target (prompts + capped AGENTS block section);
  lifecycle hooks in `app install/uninstall/--upgrade`.
- **S3** — the workspace call lane (`actions`/`call` against the
  active or pinned workspace over the OS API; cross-repo with the
  native CLI). Independently valuable.
- **S4** — workspace projection: catalog fetch at sync, pinned
  selectors, `use`-triggered reconcile, cap + skip report.
- **S5 (maybe)** — richer per-action arg schemas inline in SKILL.md if
  agents measurably fumble `--input-json` shapes.

## 12. Rejected alternatives

- **Fat bundles per app (reuse `generate_skill` wholesale)** — copies
  of the capability graph in agent homes drift from the store; the
  runner lane duplicates what `call` already governs.
- **One skill per action** — catalog bloat, and effect/confirmation
  context lives naturally in the per-app catalog.
- **Ambient-context commands in workspace skills** — silent
  retargeting across `use`; pinned selectors keep an agent's
  invocations meaning what the skill said.
- **Event-only (no reconciler)** — missed events leave permanent
  drift; convergence is the platform's established pattern.
- **Registering skills from inside the OS/desktop** — agent homes are
  per-user machine state; the SDK CLI is the thing that already owns
  writes there.

## 13. Open questions

1. Consent UX: one recorded yes at first sync vs piggybacking on bare
   `skill install` — and where the toggle lives (`~/.tangram/config`?).
2. Should machine and workspace copies of the SAME app dedupe (prefer
   local) or both project with distinct names?
3. Codex AGENTS block growth: per-app lines vs one generic line +
   prompts only — measure catalog routing quality before deciding.
4. Whether `skill sync` should also refresh the two authoring skills
   (fold `skill install` into the same reconciler eventually).
