# Installed Apps as Agent Skills — Design

Status: DRAFT for review — no implementation yet.
Companion to `mcp-server-design.md`: two projections of one governed
lane. Skills serve shell-capable agents (Claude Code, Codex CLI); MCP
serves shell-less hosts.

## 1. Problem

Agents route requests by what is in their context. Skill names and
descriptions are always there (Claude Code and Codex both load their
Agent Skills catalogs each session), so a
`tangram-todo-list` skill described as "manage todos in the installed
Todo app" matches *"list my todos"* by domain words — routing the
generic usage prose we ship in AGENTS.md cannot provide. Today nothing
projects the store into agent homes: installing an app changes what the
machine can do but not what agents know.

The same gap exists one level up: in the v3 context model the SAME
verbs target a remote Tangram OS workspace after `tangram use <ctx>`.
Apps installed in that workspace should reach agents the same way.

## 2. Goals

- Once projection is enabled (a single explicit opt-in, §9),
  installing an app makes it *discoverable and usable* by Claude Code
  and Codex with no further manual steps; uninstalling removes it;
  upgrading refreshes it.
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

- `SKILL.md` — frontmatter (name, description, Tangram fields under
  `metadata:`) + body: what the app is, the action catalog (ref,
  effect, requiresConfirmation, doc), and the exact CLI loop for THIS
  app: `tangram-app actions <id>`, **`inspect <id> --action <Ref>` for
  the argument schema** (the `actions` catalog carries no schemas —
  without this step agents cannot build nontrivial `--input-json`),
  then `call <id> <Ref> --local --input-json '{…}'` with mutation
  flags and the never-curl rule.

**Rendering inputs must exist first.** Today `.install.json` records
only id/version/root/source/timestamp and the capability graph has no
manifest description — the fields this design renders are not in the
store record. Decision: `app install`/`--upgrade` (which already
validate the package) write a validated **projection block** into
`.install.json` (description, action catalog summary, digest);
`skill sync` renders from the record only and never recompiles.
Installs predating the block are skipped with a note in the sync
report; `skill sync --rebuild` recompiles them once to backfill. A
corrupt record skips that app and reports it.

**Drift guard at call time, not just metadata.** A digest in
frontmatter alone protects nothing — `call` executes whatever
currently occupies the store path, and an agent session can hold a
projected skill's content after the file changed. The taught commands
therefore embed `--package-digest sha256:…`, a NEW `call`/`inspect`
flag (S1 scope) that refuses on mismatch with a
`skill sync`-and-retry hint. This restores the lock property the fat
`generate_skill` bundle has today.

Frontmatter (ownership under the portable `metadata:` field — custom
top-level keys are not part of the shared Agent Skills format):

```yaml
name: tangram-todo-list
description: …
metadata:
  managed-by: tangram-app-sdk
  tangram-app: com.example/todo-list
  tangram-context: machine            # or instance/workspace
  package-digest: sha256:…
```

**Ownership is per skill DIRECTORY, checked entry by entry.** A
directory is managed only when its `SKILL.md` carries the marker AND
every entry in it is one the renderer writes; anything else (extra
user files, a symlinked SKILL.md, a path escaping the skills root) is
a reported conflict, never rewritten or deleted. Writes are atomic
(temp file + rename), symlinks are refused as targets — the
`_write_skill` invariants carry over.

**Naming must survive the store's identity model.** Apps are keyed by
`group/name` and two groups can install the same bare name;
`normalize_skill_name` also truncates to 63 chars. A rule that depends
on the CURRENT desired set ("bare when unique, else hashed") is not
stable — installing an unrelated same-named app would rename an
existing skill. So the assignment is PERSISTED: at first projection an
app gets `tangram-<name>` if free, else
`tangram-<name>-<8-hex-of-sha256(id)>`, recorded in
`~/.tangram/config.json`. When one sync introduces several colliding
apps at once, allocation runs in sorted-full-id order, so fresh
configs and `--rebuild` converge identically. An allocation outlives
context absence — a workspace switching away keeps its names reserved,
so returning never renames anything; allocations are released only on
uninstall (machine) or explicit `skill sync --prune-names`. Later
arrivals never rename earlier skills. Workspace
apps prefix the workspace: `tangram-<ws>-<name>[-<hash>]`, same
allocation rules.
Description = validated identifier-derived phrase + the app's manifest
description (see §8) + an origin suffix ("on this machine" /
"workspace <ws>") — the description is the router, so it must carry
the domain words.

## 6. Agent targets

- **Claude Code**: `~/.claude/skills/<name>/SKILL.md` (user scope —
  the store is user-scoped). Project scope is out: the store isn't
  per-project.
- **Codex**: `~/.codex/skills/<name>/SKILL.md` — Codex's discoverable
  Agent Skills catalog, the analogue of Claude's. NOT
  `~/.codex/prompts/` (a prompt is an explicit `/name` invocation, not
  catalog routing — projecting there would never enter Codex's skill
  discovery). The existing AGENTS.md marked block is unchanged by app
  projection in v1; whether per-app pointer lines help routing on
  Codex versions without skills support is an open question, measured
  not assumed. (The two AUTHORING skills continue to ship as prompts —
  migrating them to `~/.codex/skills` is folded into open question 3.)

## 7. Context switching and remote workspace apps

The v3 model: one verb table, destination decided by context
(`machine` default; `tangram use <ctx>` selects an instance/workspace).
Projection follows it:

- **Desired set = machine apps ∪ active-context workspace apps.**
  `tangram use` triggers `skill sync`; switching away removes the
  previous workspace's managed skills and projects the new one's.
- **DEPENDENCY — a canonical persisted context.** Today the python
  lane persists only an instance (`.HEAD`); the workspace is always an
  explicit flag, and `use` itself is native-only (the pip front door
  delegates it back). Before S4 there must be one recorded
  instance+workspace context both CLIs read, and the native `use` must
  invoke `skill sync` (or the front door intercepts it). Cross-repo
  work, called out as such.
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
  credential lane with the install lane's protections (no proxy, no
  redirects). There is no auto-refresh or login prompt in this lane
  today: a 401/expired credential surfaces as a structured error
  naming the fix (`tangram login`), both at sync (workspace catalog
  fetch fails → workspace skills left as-is, reported) and at call
  time.

## 8. Prompt injection (the risk that needs its own section)

Projected skill text originates in app manifests — third-party content
(hub apps; for workspaces, other members' apps) that would be injected
into EVERY agent session's context automatically. Be honest about what
filtering can do: **no sanitizer makes adversarial free-form prose
safe** — semantically equivalent instructions survive any phrase
blacklist. The design therefore separates trust tiers instead of
claiming filtering is a boundary:

- **Routing metadata defaults to validated identifiers only**: the
  skill name and the identifier-derived description phrase come from
  charset-constrained fields (app name, action refs) that cannot carry
  prose. This alone routes most domain asks ("todo-list" matches
  "todos").
- **Free-form manifest text (description, action docs) projects
  identifier-only by DEFAULT and is opt-in per content digest.** A
  one-time enable cannot authorize text that arrives later via
  upgrade, `--rebuild`, or workspace changes, and a workspace admin's
  consent does not speak for the local agent user. So: unreviewed
  free-form content never enters a skill; `skill sync --review` shows
  pending text verbatim and records approval AGAINST ITS DIGEST in
  `~/.tangram/config.json` — changed text goes back to pending and the
  skill drops to identifier-only until re-approved. Length caps and
  labeled "app says:" framing apply as hygiene — they reduce accident
  surface, they are NOT the security boundary; the boundary is that
  governed calls stay policy-gated regardless of what a skill says.
- The skill body's instructions (the CLI loop) are OURS, templated;
  app content appears only as labeled data within it.
- Workspace projection inherits workspace trust: installing an app
  into a workspace is an admin-consented act; caps, labeling, and the
  review rule still apply.

## 9. Consent and clutter

- **The consent contract is decided, not open** (it gates S1):
  projection is OFF until enabled once by an explicit, non-interactive
  command — `tangram-app skill sync --enable` (recorded in
  `~/.tangram/config.json`; works in scripts and CI, no TTY dance).
  Until then, lifecycle hooks print a one-line hint and succeed
  WITHOUT projecting — an install never fails because projection
  couldn't run. `skill sync --status` is read-only always.
  `app install --no-skills` opts a single install out;
  `skill sync --disable` turns it back off and removes managed
  entries.
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

- **S1** — machine-context `skill sync` + Claude Code target:
  projection block written into `.install.json` at install/upgrade
  (+ `--rebuild` backfill), managed SKILL.md rendering under the §8
  trust tiers, `call`/`inspect --package-digest` refuse-on-mismatch
  flag, reconciler with directory-ownership rules + deterministic
  naming, the §9 consent contract (`--enable`/`--disable`/`--status`),
  unit tests over a store fixture.
- **S2** — Codex target (`~/.codex/skills`); lifecycle hooks in
  `app install/uninstall/--upgrade`.
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

1. Should machine and workspace copies of the SAME app dedupe (prefer
   local) or both project with distinct names?
2. Do Codex versions without Agent Skills support need per-app
   AGENTS.md pointer lines as a fallback — measure routing quality
   before adding block growth.
3. Whether `skill sync` should also own the two authoring skills
   (fold `skill install` into the same reconciler, migrating the Codex
   copies from `prompts/` to `skills/`).
4. Whether the projection block in `.install.json` should be
   sha-covered by the install record itself (tamper-evidence for the
   rendered text).
