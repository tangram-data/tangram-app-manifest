# Application

**Spec revision:** 1.0 (draft)

## Identity and metadata — `app.pkl`

`manifests/app.pkl` is the entry point of every manifest package. It declares the application's identity and catalog metadata as top-level fields.

```pkl
manifestSpecVersion = "v1"
group       = "com.intuit"
name        = "quickbooks"
version     = "0.1.0"
appType     = "Connector"
category    = "finance"
tags        = List("quickbooks", "accounting", "oauth")
description = "QuickBooks Online connector: query company data, record expenses, attach receipt notes."
providerWebsite = "https://developer.intuit.com"
readme      = read("README.md").text
```

### Fields

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `manifestSpecVersion` | `String` | MUST | The manifest spec version this package targets. This revision defines `"v1"`. |
| `group` | `String` | MUST | Reverse-DNS publisher namespace (e.g. `com.intuit`, `ai.tangram`). Together with `name`, globally identifies the application. |
| `name` | `String` | MUST | Application name, unique within the group. |
| `version` | `String` | MUST | Application package version (semantic versioning RECOMMENDED). |
| `appType` | `"App" \| "Connector" \| "Agent"` | MUST | Application type. Platforms MUST reject a missing or unknown value, and MUST reject the removed pre-1.0 values (`NativeApp`, `PlatformApp`, `ConnectorApp`) with a pointer to the replacement type. |
| `description` | `String` | SHOULD | One-paragraph catalog description. |
| `readme` | `String` | SHOULD | Long-form description; conventionally `read("README.md").text`. |
| `category` | `String` | MAY | Catalog category (free-form). |
| `tags` | `List<String>` | MAY | Catalog search tags. |
| `license` | `String` | MAY | SPDX license identifier (e.g. `Apache-2.0`). |
| `providerWebsite` | `String` | MAY | The upstream provider's site (Connectors). |
| `website` | `String` | MAY | The application's own site. |
| `supportedCloudProviders` | `List<CloudProvider>` | MAY | Restricts installation to the listed providers (`Local`, `AWS`, `Azure`, `GCP`, `Scaleway`, `Ovh`). Omitted means all. |

The pair `group/name` is the application's canonical reference everywhere else in the platform: agent tool bindings (`com.acme/salesforce`), UI component catalog names (`{group}/{app}/{component}`), resource-type identities, and OAuth callback paths.

The reserved group `ai.tangram-os` identifies the platform itself; the well-known application `ai.tangram-os/core` owns the platform super types (see [Resources & Actions](resources.md#platform-super-types)). Third-party manifests MUST NOT claim the reserved group.

## Configuration — settings and secrets

Applications declare install-time configuration as typed field lists. Values are supplied by the workspace administrator at install and reach the application through deployment expressions (`getConfig`/`getSecret`) or connector header templates (`{{settings.*}}`/`{{secrets.*}}`) — never hard-coded in the manifest.

- `manifests/settings.pkl` MUST expose a top-level `settings: List<ConfigField>` expression for non-sensitive configuration.
- `manifests/secrets.pkl` MUST expose a top-level `secrets: List<ConfigField>` expression for sensitive values. Platforms MUST store these in their secret store and MUST NOT echo them back in APIs or logs.

```pkl
import "@tangram-app-manifest/manifest.pkl" as manifest

secrets = List(
  new manifest.ConfigField {
    name = "token"
    required = true
    description = "Databricks PAT token of a workspace admin"
  }
)
```

### `ConfigField`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | `String` | MUST | Key the value is supplied and referenced under. |
| `description` | `String` | MUST | Shown on the install form. |
| `required` | `Boolean` | MUST | Whether install may proceed without a value. |
| `default` | `String?` | MAY | Pre-filled default. |
| `example` | `String?` | MAY | Placeholder/example shown to the administrator. |

Note that connector *OAuth client credentials* are not workspace secrets — they are global publisher-scoped system secrets named by the OAuth declaration (see [Connectors](connector.md#client-credentials)).
