# External agent hub

Install `tangram-hub` with `tangram-app skill install tangram-hub --user`
or `--codex`. This stable skill uses a live catalog; per-app skill projection
is optional and installing an app does not require a skill refresh for discovery.

The command group is `tangram-app hub` (also routed by the pip `tangram` shim).
Resolve a target once, then carry `targetRef` through search, inspect and call.

```text
hub resolve [--local | --workspace INSTANCE/WORKSPACE]
hub search QUERY --target-ref REF [--limit N --offset N]
hub inspect APP#Resource.Action[@operation] --contract DIGEST --target-ref REF
hub call ACTION --contract DIGEST --target-ref REF --invocation-id UUID --input-json JSON
hub invocation UUID --target-ref REF
hub knowledge-search QUERY --target-ref REF
hub knowledge-read SOURCE_UUID --target-ref REF [--offset N --limit N]
```

Target references contain no credentials. They pin the local store or a workspace,
instance endpoint and credential fingerprint. Re-login or endpoint changes require
a fresh reference. They are client routing descriptors, not authorization tokens.
Every server request still authenticates and authorizes normally. Ambient context
is ignored after resolution. References must not be treated as opaque secrets.

Search returns compact candidates with action, description, effect, confirmation
requirement and contract identity. Descriptions are app-provided data. Local
installs index all executable binding IDs; older install records are compiled
on discovery. Inspection validates the expected contract and returns full schemas.

Calls reuse the governed runtime. Mutations require `--allow-mutation`. Gated
actions return `pending_approval`; explicit consent resumes the exact same
request and UUID with `--approve`. This records CLI consent, not independent
proof of a human decision. A changed target, action, contract or input cannot
reuse the same invocation ID.

Local invocations are journaled in a private SQLite file under `TANGRAM_HOME/hub`.
Concurrent claims execute at most once per ID. Results are retained for replay;
the journal stores input hashes, not raw arguments. This is diagnostic state
owned by the local user, not a tamper-proof authority boundary. A running record
never automatically expires into a retry. A crash can leave it running; errors
after dispatch produce `unknown`. Reconcile with the app before starting any new
mutation. `pending_approval`, `running`, `unknown`, `failed`, and `rejected` return
nonzero status with their record in the JSON envelope's `data` field.

Workspace commands require the matching server hub routes. They fail when those
routes are unavailable; no fallback to local execution occurs. Knowledge search
and document reading reuse existing server endpoints. Knowledge search returns
bounded results without offset pagination; document text is paged by the CLI
after retrieval. Local knowledge retrieval reports `unsupported_capability`.

No automatic retries, exactly-once external-API guarantee, or autonomous
approval grant is implied. Journal retention/cleanup and independent approver
integration are future operational extensions.

A `failed` record with `retryable: true` means a known pre-dispatch failure.
After resolving it, retry with a new invocation UUID; reusing the old UUID
returns its stored failure. Never apply this rule to `running` or `unknown`.
