# Example usage

```pkl
import "@tangram-app-manifest/manifest.pkl" as manifest

secrets = List(
  new manifest.ConfigField {
    name = "token"
    required = true
    description = "Databricks PAT token of a user who has admin permissions of the target databricks workspace"
  }
)
```

## Modules

- `manifest.pkl` — app identity, app types, and shared configuration fields
- `resource-types.pkl` — resources, actions, mappings, roles, and privileges
- `integration.pkl` — integration interfaces and implementations
- `connector.pkl` — request authentication headers
- `oauth.pkl` — OAuth connection lifecycle and scopes
- `deployment.pkl` — deployment, telemetry, component, and Helm schemas
- `deployment-runtime.pkl` — injected deployment context and runtime helpers
- `ui.pkl` — UI components and proxy URI patterns
- `agent.pkl` — agent model, tool, and skill declarations
- `infra.pkl` — infrastructure claims and injected resource access
- `app-package.pkl` — workspace-built app package schema

`core.pkl` was removed in `2.0.0`; consumers must import the owning modules
directly.

# OAuth-backed connectors

OAuth declarations live in the standalone `oauth.pkl` module. Connector
request-authentication templates live in `connector.pkl` because API-key and
OAuth-backed connectors share them.

```pkl
import "@tangram-app-manifest/connector.pkl" as connector
import "@tangram-app-manifest/oauth.pkl" as oauthTypes

auth = new connector.ConnectorAuth {
  httpHeaders = Map(
    "Authorization",
    new connector.HeaderTemplate { template = "Bearer {{oauth.accessToken}}" }
  )
}

oauth = new oauthTypes.OAuth2AuthCode {
  authorizationUrl = "https://provider.example.com/oauth/authorize"
  tokenUrl = "https://provider.example.com/oauth/token"
  scopes = List("records.read")
  clientIdSecret = "providerClientId"
  clientSecretSecret = "providerClientSecret"
  callbackPath = "/api/core/v1/connector-oauth/callback/com.example/records"
  connectionScope = new oauthTypes.WorkspaceOnly {}
}
```

# Publishing UI components
An app publishes reusable UI components by shipping `manifests/ui/components.pkl`
(amends `ui.pkl`; see `examples/ui-components/`):
```
amends "@tangram-app-manifest/ui.pkl"

components {
  new AppUIComponentSpec {
    name = "top-accounts"
    kind = "declarative"
    spec = "ui/components/top-accounts.json"
    surfaces { "chat"; "dashboard"; "app-page" }
  }
  new AppUIComponentSpec {
    name = "churn-explorer"
    kind = "sandboxed"
    entry = "ui/components/churn-explorer.tsx"                  // compiled to a bundle at publish
    spec = "ui/components/churn-explorer.contract.json"         // optional data contract
    surfaces { "chat"; "dashboard"; "app-page" }
  }
}
```
For `kind = "sandboxed"`, `entry` is the TSX/JSX source and the optional `spec`
is a contract JSON declaring `inputs`, named `bindings` (semantic/sql/action),
`outputs`, and optionally `title` — never `bundle` or `view` (the platform fills
`bundle.ref` from the compiled entry).

`./gradlew evalUiComponentsExample` renders the example the way the platform loader does.

# Releasing

Releases are cut by pushing a tag named `tangram-app-manifest@<version>`;
CI (`.github/workflows/release.yaml`) runs `./gradlew createPackage` and
uploads the package to the matching GitHub release. Before tagging:

1. `./gradlew verifyContractFixture evalUiComponentsExample evalAppPackageExample evalOAuthConnectorExample`
2. If the contract fixture diff is intentional schema drift, regenerate with
   `./gradlew updateContractFixtureSnapshot` and review the diff in the commit.
3. Bump the major version for any change to a serialized field name, a
   discriminator value, or a removed/renamed module — consumers pin exact
   versions.

Consumers depend on the released package via:

```pkl
dependencies {
  ["tangram-app-manifest"] {
    uri = "package://pkg.pkl-lang.org/github.com/tangram-data/tangram-app-manifest/tangram-app-manifest@<version>"
  }
}
```

After publishing, re-run `pkl project resolve` in each consuming repo so
`PklProject.deps.json` records the published artifact's checksum.

# Reference
[pkl-lang: Package Import](https://pkl-lang.org/main/current/language-reference/index.html#import-clause)
