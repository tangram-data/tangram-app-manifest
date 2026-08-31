---
name: tangram-connector-builder
description: Build a Tangram CONNECTOR app that maps an external SaaS HTTP API (Gmail, Slack, QuickBooks, Notion style) into governed typed actions with OAuth handled by the platform. Use when asked to create or modify a connector, integrate a third-party API/service into Tangram, or wire vendor OAuth. For apps with their own backend/database/UI use tangram-app-builder instead.
---

# Build a Tangram connector

A connector is a **manifest-only** Tangram app (`appType = "Connector"`):
Pkl manifests + an OpenAPI file describing a subset of a VENDOR's HTTP API.
There is no backend to write — the Tangram OS gateway executes every action
against the vendor endpoint, injecting OAuth credentials the platform
manages. Your job is modeling: which vendor operations become which typed
actions, with what privilege/effect/confirmation posture, under which
OAuth scopes.

This skill is self-contained. Canonical real connectors to imitate when in
doubt: `github.com/tangram-data` — gmail/calendar/docs (Google), a
QuickBooks connector (tenant-path style).

## Prerequisites

- Pkl CLI on PATH (`pkl --version`).
- The Python SDK for validation: `python3 -m tangram_app --help`; if
  missing, `pip install tangram-app-sdk` (or `pip install ./python-sdk`
  from a tangram-app-manifest checkout).

## Package layout

```text
my-connector/
└── manifests/
    ├── PklProject              # schema package dep (same as tangram-app-builder)
    ├── PklProject.deps.json    # generated: `pkl project resolve`
    ├── app.pkl                 # appType = "Connector"
    ├── secrets.pkl             # workspace-entered secrets (optional)
    └── api/
        ├── spec.pkl            # endpoint + auth + oauth
        ├── resources.pkl       # typed actions over vendor operationIds
        └── open_api.yml        # the vendor-API subset you model
```

`manifests/PklProject` is identical to the app-builder one (dependency on
`package://pkg.pkl-lang.org/github.com/tangram-data/tangram-app-manifest/tangram-app-manifest@1.0.0`,
then `pkl project resolve` inside `manifests/`).

## app.pkl

```pkl
manifestSpecVersion = "v1"
group = "com.google"          // the VENDOR's reversed domain
name = "gmail"
version = "0.1.0"
appType = "Connector"
category = "communication"
description = "Gmail connector: search, read, draft, and send email via the Gmail API."
tags = List("google", "gmail", "email", "oauth")
```

## api/spec.pkl — endpoint, auth, OAuth

```pkl
import "@tangram-app-manifest/connector.pkl" as connector
import "@tangram-app-manifest/oauth.pkl" as oauthTypes

endpoint = "https://gmail.googleapis.com"
endpointOverridable = false
endpointRequired = false
endpointHostAllowlist = List("gmail.googleapis.com")

apiSpecFile = "open_api.yml"

auth = new connector.ConnectorAuth {
  httpHeaders = Map(
    "Authorization",
    new connector.HeaderTemplate { template = "Bearer {{oauth.accessToken}}" }
  )
}

oauth = new oauthTypes.OAuth2AuthCode {
  authorizationUrl = "https://accounts.google.com/o/oauth2/v2/auth"
  tokenUrl = "https://oauth2.googleapis.com/token"
  revocationUrl = "https://oauth2.googleapis.com/revoke"
  scopes = List("https://www.googleapis.com/auth/gmail.readonly")
  clientIdSecret = "googleClientId"        // NAMES of publisher secrets,
  clientSecretSecret = "googleClientSecret" // never the values
  callbackPath = "/api/core/v1/connector-oauth/callback/com.google/gmail"
  tokenAuthMethod = "ClientSecretPost"     // or ClientSecretBasic
  additionalAuthorizeParams = List(
    // Google-specific: refresh tokens need offline + forced consent.
    new oauthTypes.AuthorizeParam { name = "access_type"; value = "offline" },
    new oauthTypes.AuthorizeParam { name = "prompt"; value = "consent" }
  )
  pkce = true
  refreshWindowSeconds = 600
  connectionScope = new oauthTypes.SelectableScope {
    default = "PerUser"                    // each user connects their own account
    allowedAtInstall = List("PerUser", "WorkspaceShared")
  }
}
```

- `callbackPath` convention: `/api/core/v1/connector-oauth/callback/<group>/<name>`.
- Tenant-scoped vendors (QuickBooks-style `realmId`) capture the tenant at
  callback and reference `{{oauth.tenantId}}` in paths — see
  `spec/connector.md` "Tenant capture".

## api/resources.pkl — typed actions

Same typed classes as app authoring; what matters here is the safety
posture per action:

```pkl
import "@tangram-app-manifest/resources.pkl" as resources

types: List<resources.ResourceTypeDefinition> = List(
  new resources.ResourceTypeDefinition {
    name = "Message"
    activeVersion = "v1"
    doc = "An email message in the connected Gmail mailbox."
    versions = List(new resources.ResourceTypeVersion {
      version = "v1"
      served = true
      actions = List(
        new resources.Action {
          name = "List"
          privilege = "Read"
          effect = "Stateless"
          idempotent = true
          doc = "Search the mailbox with a Gmail query string."
          openApiMapping = new resources.OpenApiMapping { operationId = "listMessages" }
        },
        new resources.Action {
          name = "Send"
          privilege = "Create"
          effect = "Irreversible"          // leaves the system: cannot undo
          idempotent = false
          requiresConfirmation = true      // user must approve each send
          doc = "Send an email as the connected account."
          openApiMapping = new resources.OpenApiMapping { operationId = "sendMessage" }
        }
      )
      presetRoles = List(
        new resources.Role { name = "user"; permissions = List("*"); description = "Full use of this type" }
      )
    })
  }
)
```

Posture rules: reads are `Stateless`; anything that leaves the system
(send, post, delete-at-vendor) is `Irreversible` + `requiresConfirmation =
true`. A binding can ADD a confirmation gate but never remove an
action-level one. Request the MINIMAL OAuth scopes the declared actions
need — scopes are consent-screen-visible and audited.

## api/open_api.yml — the vendor subset

Model only what your actions bind (OpenAPI 3.x; each `openApiMapping`
`operationId` must exist). Pin the connection identity into paths — e.g.
Gmail uses `/gmail/v1/users/me/...` so no caller can address another
mailbox. Input/output schemas here become the actions' tool schemas: keep
them small and typed. Vendor host quirks are real: some products serve the
API from a different host than the product domain (Google Drive/Calendar
use `www.googleapis.com`; `drive.googleapis.com` answers HTML 404s).

## Verify loop

```sh
cd manifests && pkl project resolve && cd ..
python3 -m tangram_app validate .          # structured findings
python3 -m tangram_app inspect . --tools   # actions as the agent will see them
```

HONEST LIMITS: a connector cannot execute standalone — OAuth lives in
Tangram OS, so `call --local` is unavailable. Full e2e = install on a
Tangram OS workspace, connect an account, invoke through the gateway. The
native `tangram` CLI (`tangram app manifest validate .`) is the publishing
conformance authority when available.

## Gotchas

- **Always use the typed classes.** In untyped `Dynamic` blocks, `default`
  is a built-in Pkl property and SILENTLY DROPS from rendered JSON — the
  `connectionScope.default` field vanishes without error.
- `clientIdSecret`/`clientSecretSecret` are secret KEY NAMES resolved by
  the platform, never credential values in the manifest.
- Refresh-token issuance is vendor-quirky (Google needs
  `access_type=offline` + `prompt=consent`); check the vendor's docs.
- `secrets.pkl` (workspace-entered secrets) is
  `import "@tangram-app-manifest/manifest.pkl" as m` +
  `secrets: List<m.ConfigField> = List(...)`; omit the file when OAuth
  covers everything.
- Sandboxed UI components (`ui/`) are an advanced, optional layer (see the
  gmail connector); adding them pulls in UI deployment requirements —
  ship the manifest-only connector first.
