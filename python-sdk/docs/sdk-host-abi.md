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
- Authentication: `Authorization: Bearer <token>` — the backend service
  token (`TANGRAM_SERVICE_TOKEN`) on the platform; the per-session actions
  token (`TANGRAM_LOCAL_ACTIONS_TOKEN`) on the standalone host.
- Client timeout: 60s per request. Retry policy: ONLY `503` responses are
  retried (0.5s backoff, 30s deadline) — every operation must therefore be
  treated as non-idempotent by hosts on non-503 paths.

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

Size limits: storage.put ≤ 8 MiB content; standalone actions request body
≤ 1 MiB. Hosts answer `413` beyond limits.

## Error envelope

Canonical (structured):

```json
{"error": {"code": "<stable-code>", "message": "<safe text>", "retryable": false}}
```

Protocol-1 legacy (platform routes, string form): `{"error": "<text>"}`.
The module normalizes BOTH into `tangram.ActionError(code, message,
retryable)`; the string form maps to code `action_failed`. Callers branch
on `code`, never message text.

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
- Hosts never echo secrets or tokens in error messages.
