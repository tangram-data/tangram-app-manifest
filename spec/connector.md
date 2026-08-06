# Connectors

**Spec revision:** 1.0

A `Connector` integrates a remote system. It deploys nothing of its own by default: the platform proxies its [OpenAPI-declared operations](api.md) to an upstream endpoint, injecting credentials per request. The Connector form of `manifests/api/spec.pkl` declares the upstream, the request authentication, and (optionally) an OAuth 2.0 connection lifecycle.

## Connector form of `api/spec.pkl`

```pkl
import "@tangram-app-manifest/connector.pkl" as connector
import "@tangram-app-manifest/oauth.pkl" as oauthTypes

endpoint = "https://quickbooks.api.intuit.com"
endpointOverridable = true
endpointRequired = false
endpointHostAllowlist = List("quickbooks.api.intuit.com", "sandbox-quickbooks.api.intuit.com")
apiSpecFile = "open_api.yml"

auth = new connector.ConnectorAuth {
  httpHeaders = Map(
    "Authorization", new connector.HeaderTemplate { template = "Bearer {{oauth.accessToken}}" },
    "Accept",        new connector.HeaderTemplate { template = "application/json" }
  )
}

oauth = new oauthTypes.OAuth2AuthCode { … }
```

### Fields

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `apiSpecFile` | `String?` | SHOULD | OpenAPI document path, relative to `manifests/api/`. |
| `auth` | `ConnectorAuth` | MUST | Request authentication (below). |
| `endpoint` | `String?` | MAY | Default upstream base URL. |
| `endpointOverridable` | `Boolean` | MAY (default `false`) | Whether installs may override the endpoint. |
| `endpointRequired` | `Boolean` | MAY (default `false`) | Whether installs MUST supply an endpoint (for products with per-customer hosts). |
| `endpointHostAllowlist` | `List<String>` | SHOULD when overridable | Hosts an override may point at. Platforms MUST reject overrides outside a non-empty allowlist. |
| `oauth` | `OAuth2AuthCode?` | MAY | OAuth 2.0 authorization-code declaration (below). |

## Request authentication

`ConnectorAuth.httpHeaders` maps header names to templates evaluated **at request time** and injected into every forwarded call. Template variables:

| Variable | Source |
|---|---|
| `{{secrets.<key>}}` | Workspace secret declared in `secrets.pkl` (API-key connectors) |
| `{{settings.<key>}}` | Workspace setting declared in `settings.pkl` |
| `{{oauth.<key>}}` | The live OAuth connection — `accessToken`, `tenantId`, … |

Credentials therefore never appear in the manifest, in client requests, or in the client-facing API surface — only the proxy sees them.

## OAuth 2.0 — `OAuth2AuthCode`

Declares an authorization-code flow the platform runs on the connector's behalf: it hosts the callback, exchanges and refreshes tokens, stores connections per the declared scope, and exposes token values to header/path templates.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `authorizationUrl` | `String` (`https://` only) | MUST | Provider authorize endpoint. |
| `tokenUrl` | `String` (`https://` only) | MUST | Token endpoint, used for the initial code exchange and refresh grants. |
| `revocationUrl` | `String?` (`https://` only) | SHOULD | When set, disconnect calls it best-effort before clearing local tokens, so consent does not linger upstream. |
| `scopes` | `List<String>` | SHOULD | Scopes requested at authorize time. The platform compares against granted scopes on each connection load and flags `scope_drift` when the manifest gains an ungranted scope. |
| `clientIdSecret` | `String` | MUST | Name of the **global system secret** holding the publisher's `client_id` (see below). |
| `clientSecretSecret` | `String` (≠ `clientIdSecret`) | MUST | Name of the global system secret holding the `client_secret`. |
| `callbackPath` | `String` | MUST | Path the platform serves the callback on and sends as `redirect_uri`. MUST have the canonical shape `/api/core/v1/connector-oauth/callback/{group}/{name}` matching this application — drift makes the provider reject every callback. |
| `tokenAuthMethod` | enum | MAY (default `ClientSecretBasic`) | How the platform authenticates to the token endpoint: `ClientSecretBasic` (RFC 6749 §2.3.1 Basic header), `ClientSecretPost` (form body), `None` (PKCE-only public clients). `PrivateKeyJwt` and `TlsClientAuth` are schema-valid but rejected at install as planned-for-later. |
| `additionalAuthorizeParams` | `List<AuthorizeParam>` | MAY | Extra ordered `name=value` parameters on the authorize URL (`prompt=consent`, `audience=…`). Ordered list, not a map — some providers care about parameter order. The platform adds `response_type=code` automatically. |
| `tenant` | `TenantSource?` | MAY | How the upstream tenant id is captured (below). |
| `pkce` | `Boolean` | MAY (default `true`) | PKCE (RFC 7636). Disable only for providers that do not support it. |
| `refreshWindowSeconds` | `Int` (0 < n < 3600) | MAY (default `600`) | Refresh tokens this long before expiry. The 10-minute default cushions clock skew; small windows routinely lose to skew and force 401-driven retries on the proxy hot path. |
| `connectionScope` | `ConnectionScope` | MAY (default `WorkspaceOnly`) | Which connection shapes the connector supports (below). |

### Client credentials

`clientIdSecret` / `clientSecretSecret` name **platform-global system secrets** provisioned by the platform operator from the publisher's OAuth app registration. They are *not* per-install workspace secrets: workspace administrators never see or type them. This is what lets one published connector serve many workspaces under a single upstream OAuth application.

### Tenant capture

Many providers scope API paths by a tenant identifier (QuickBooks `realmId`, Slack `team.id`). `TenantSource` is a discriminated union (`kind`) declaring where the platform learns it, after which `{{oauth.tenantId}}` becomes available to upstream path and header templates:

| Kind | Fields | Capture point |
|---|---|---|
| `CallbackParam` | `name` | Query parameter on the OAuth callback (`?realmId=…`) |
| `TokenResponseField` | `jsonPath` | Field in the token-exchange response body (`$.team.id`) |
| `UserInfo` | `url`, `jsonPath` | Separate userinfo endpoint after exchange *(schema-valid; not implemented in v1)* |
| `RequiredAtInstall` | `settingName` | Install-time setting supplied before OAuth runs *(schema-valid; not implemented in v1)* |

### Connection scope

`ConnectionScope` (discriminated union on `kind`) governs how many connections exist and who they act as:

| Declaration | Mode(s) | Typical use |
|---|---|---|
| `WorkspaceOnly` | `WorkspaceShared` — one connection per workspace, acts as an admin | Stripe; single-company bookkeeping |
| `PerUserOnly` | `PerUser` — one connection per user | Google Drive, GitHub — privacy matters |
| `NamedTenantOnly` | `NamedTenant` — N labelled connections per workspace | Agencies managing many company files *(declared ahead of runtime support; connection starts rejected in v1)* |
| `SelectableScope` (`kind = "Either"`) | Admin picks at install from `allowedAtInstall`; `default` is pre-selected | Mixed populations |

`SelectableScope` constraints (enforced in the schema and again by the platform validator): `allowedAtInstall` MUST be non-empty and MUST contain `default`. The v1 runtime can start `WorkspaceShared` and `PerUser` connections; `NamedTenant` and `PerUserPerTenant` are valid declarations but MUST be rejected explicitly at connection start until implemented.

## Worked example

See the [QuickBooks connector](https://github.com/tangram-data/tangram-app-hub) for a complete Connector manifest: OAuth with `CallbackParam` tenant capture, `SelectableScope` between `WorkspaceShared` and `PerUser`, endpoint override allow-listed to the Intuit sandbox host, and every upstream path rewritten through `x-tangram-upstream` with `{{oauth.tenantId}}`.
