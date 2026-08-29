# SDK ↔ host ABI, protocol 1

[Back to the SDK guide](sdk-guide.md)

The in-backend `tangram` module talks to its host over loopback HTTP. This
ABI is **private but versioned**: app code must never call it directly (the
`tangram` module is the public surface), yet every operation is specified
here so the two host implementations — the Tangram OS platform routes and
the standalone local host — cannot drift. The behavioral conformance
artifact (`conformance/backend-sdk-contract-1.json`, sha-pinned in both
repos) enforces the module surface; this document freezes the wire.

## Versioning

- The module constants `tangram.PROTOCOL` (wire major, `"1"`) and
  `tangram.__version__` (module release) identify the caller.
- Every SDK request carries `X-Tangram-SDK-Protocol: <major>`.
- Hosts SHOULD echo `X-Tangram-SDK-Protocol` on responses. In protocol 1 an
  absent echo is read as `1`; a present echo with a different major is
  refused by the module with error code `protocol_mismatch`. Hosts likewise
  refuse request majors they do not speak.
- Compatibility matrix: same major = compatible (minor evolution is
  additive: new operations, new optional request fields, new response
  fields). Different major = refuse loudly at the first call.

## Transport

- Loopback (or in-cluster service) HTTP; `POST` only; request and response
  bodies are `application/json`.
- Authentication:
  - **Platform:** the module sends NO `Authorization` header and never sees
    the service token — the runtime supervisor pops `TANGRAM_SERVICE_TOKEN`
    from the environment before app code loads and injects
    `Authorization: Bearer <token>` at its loopback SDK proxy
    (`TANGRAM_SDK_URL` points at the proxy). Auth is out-of-band by design:
    app code cannot exfiltrate a credential it never holds.
  - **Standalone:** `Authorization: Bearer <TANGRAM_LOCAL_ACTIONS_TOKEN>`,
    sent by the module.
- Client timeout: 60s per request. Retry policy (protocol 1): the
  STANDALONE module retries `503` on every standalone-served operation
  (0.5s backoff, 30s deadline, the host-starting window); the platform
  module performs no client-side retry. Hosts must treat every operation
  as non-idempotent.

## Environments

| Env var | Meaning |
|---|---|
| `TANGRAM_SDK_URL` | Platform host base (routes below under `/api/core/v1/workspaces/{ws}/apps/{app}/backend/sdk/`) |
| `TANGRAM_LOCAL_ACTIONS_URL` | Standalone host base (serves `/actions/invoke` only) |
| `TANGRAM_LOCAL_ACTIONS_TOKEN` | Standalone bearer token |
| `TANGRAM_WORKSPACE`, `TANGRAM_APP`, `TANGRAM_SERVICE_TOKEN`, `TANGRAM_DB_*` | Identity + own-database coordinates |

## Operations

| Operation | Platform path (suffix) | Standalone | Request | Success response |
|---|---|---|---|---|
| storage.put | `storage/put` | — | `{path, content_base64}` | `{path}` |
| storage.get | `storage/get` | — | `{path}` | `{content_base64}` |
| storage.list | `storage/list` | — | `{prefix?}` | `[{path, folder, size}]` |
| storage.delete | `storage/delete` | — | `{path}` | `{deleted}` |
| storage.presign | `storage/presign` | — | `{path, method, expires_seconds}` | `{url, expiresSeconds}` |
| secrets.get | `secrets/get` | — | `{key}` | `{value}` |
| actions.invoke | `actions/invoke` | `/actions/invoke` | `{resource_type, action, args, app?}` | `{result}` |
| sql.run | `sql/run` | — | `{name, params}` | `{rows, truncated}` |
| schedules.create | `schedules/create` | `/schedules/create` | `{name, resource_type, action, args?, cron? \| every? \| at?, timezone?}` | `{schedule}` |
| schedules.list | `schedules/list` | `/schedules/list` | `{}` | `{schedules}` |
| schedules.delete | `schedules/delete` | `/schedules/delete` | `{name}` | `{deleted}` |
| schedules.pause | `schedules/pause` | `/schedules/pause` | `{name}` | `{paused}` |
| schedules.resume | `schedules/resume` | `/schedules/resume` | `{name}` | `{resumed}` |
| schedules.runs | `schedules/runs` | `/schedules/runs` | `{name, limit?}` | `{runs}` |
| notifications.send | `notifications/send` | — | `{to, subject, body, link?, channel?, dedupe_key?}` | `{id, queued, skipped, deduped?}` |
| notifications.list | `notifications/list` | — | `{limit?}` | `{notifications}` |

Size limits: storage.put ≤ 8 MiB content; standalone actions request body
≤ 1 MiB. Hosts answer `413` beyond limits.

## Error envelope

Canonical (structured):

```json
{"error": {"code": "<stable-code>", "message": "<safe text>", "retryable": false}}
```

Protocol-1 legacy (platform routes, string form): `{"error": "<text>"}`.
Both module implementations normalize BOTH forms into
`tangram.ActionError(code, message, retryable)` — an identical public
class shape — honoring only correctly typed envelope fields (anything
malformed degrades to `action_failed` / safe message / `retryable: false`).
The string form maps to code `action_failed`. Callers branch on `code`,
never message text. A protocol-major mismatch detected on ANY response
(success or error) raises code `protocol_mismatch`.

Codes (HTTP mapping): `unauthenticated` (401), `not_found` (404),
`payload_too_large` (413), `host_starting` (503, retryable),
`invalid_request` (400), `cross_app_unsupported` (400),
`confirmation_required_unattended` (403), `protocol_mismatch` (400),
`action_failed` (400, catch-all).

## Semantics guarantees

- `actions.invoke` is unattended: hosts refuse irreversible or
  confirmation-gated actions (`confirmation_required_unattended`) — there
  is no approval surface behind this ABI.
- `actions.invoke` with `app` set targets a DECLARED backend dependency on
  the platform; the standalone host answers `cross_app_unsupported`.
- `sql.run` executes only statements approved via `declare_backend_query`;
  the request never carries SQL text.
- `schedules.create` requires the approved `declare_backend_scheduling`
  capability (the other schedule operations require ownership only — the
  scheduler itself stops firing fail-closed when the capability is gone)
  and only ever targets the app's OWN unattended-eligible actions
  (`create` upserts by name with exactly one of `cron` — standard 5-field
  Unix cron or Quartz, evaluated in `timezone` — `every` — a fixed
  interval `<n><s|m|h|d>` like `"30m"`/`"90m"`/`"2h"`, first fire
  immediately then every interval; use this for interval cadence instead
  of hand-built `*/N` cron, which is invalid for N>59 — or `at`, a future
  ISO instant; args are frozen, size-capped, references-not-secrets). Fires are at-most-once
  per window, carry a `schedule-run-<id>` invocation id for dedup, and
  repeated consecutive failures autopause the schedule until
  `schedules.resume`. The STANDALONE host serves this surface with a
  host-side scheduler (never in app code) firing through the same
  unattended actions pipeline as `actions.invoke`, with deliberate
  divergences: fires happen only while a run session is up and missed
  windows collapse into ONE fire; `cron` accepts standard 5-field Unix
  only (numeric atoms — Quartz, names, `?`/`L` are refused
  `invalid_request`); the scheduling capability is treated as granted;
  autopause threshold is 5 consecutive failures; `runs` keeps the last 20;
  `args` are capped at 32 KiB; state persists in the project's
  `.preview/schedules.json`.
- `notifications.send` requires the approved `declare_backend_notifications`
  capability and addresses WORKSPACE MEMBER account ids only — the request
  never carries an email address or Slack id (address-shaped values are
  refused), and the platform resolves addresses and delivers, attributed to
  the app. Delivery is asynchronous and at-most-once: `send` answers the
  enqueue preview (`queued` / `skipped` with coarse reasons `not-a-member`
  | `unreachable`), every gate re-runs at dispatch, and ambiguous provider
  outcomes land terminal `unknown` in `notifications.list`, never retried.
  `dedupe_key` pins the exact request by digest inside a fixed window —
  same key + different content is an error. The standalone host has no
  wire surface for notifications: the staged module implements both
  operations IN-MODULE as developer desktop notifications (macOS
  `osascript`, Windows toast, Linux `notify-send`) — one native
  notification per `send`, titled with the app id; recipients are echoed
  `queued` without member resolution; explicit `email`/`slack` answer
  every recipient skipped `unreachable`; a failed notifier lands terminal
  `failed` in `notifications.list`; the dedupe window is the process
  lifetime. Envelope shapes match the platform rows above so app code
  ports unchanged.
- Hosts never echo secrets or tokens in error messages.
