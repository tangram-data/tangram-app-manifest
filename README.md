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

- `manifest.pkl` — app identity, the `App`/`Connector`/`Agent` types, and shared configuration fields
- `resources.pkl` — resource types, actions, mappings, roles, and privileges
- `integration.pkl` — integration interfaces and implementations
- `connector.pkl` — request authentication headers
- `oauth.pkl` — OAuth connection lifecycle and scopes
- `api.pkl` — the producer-agnostic `ServiceBackend` API binding
- `deployment.pkl` — deployment, telemetry, component, source-artifact, and Helm schemas
- `deployment-runtime.pkl` — injected deployment context and runtime helpers
- `ui.pkl` — UI components and proxy URI patterns
- `agent.pkl` — agent model, tool, and skill declarations
- `infra.pkl` — infrastructure claims and injected resource access

`core.pkl` was removed in `2.0.0`; consumers must import the owning modules
directly. The unification release removed `app-package.pkl` (the built-app
package vocabulary) together with the `NativeApp`/`ConnectorApp`/`PlatformApp`
application types — every application is an `App`, `Connector`, or `Agent`
(see `docs/app-manifest-unification-design.md` in the tangram repo).

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
An app publishes reusable UI components through the `components` property in
`manifests/ui/spec.pkl` (see `examples/ui-components/`):
```
import "@tangram-app-manifest/ui.pkl" as ui

components: Listing<ui.AppUIComponentSpec> = new Listing {
  new ui.AppUIComponentSpec {
    name = "top-accounts"
    kind = "declarative"
    spec = "components/top-accounts/top-accounts.json"
    surfaces { "chat"; "dashboard"; "app-page" }
  }
  new ui.AppUIComponentSpec {
    name = "churn-explorer"
    kind = "sandboxed"
    // `components/<component-name>/<entry-file>`; compiled to a bundle at publish
    entry = "components/churn-explorer/churn-explorer.tsx"
    spec = "components/churn-explorer/churn-explorer.contract.json" // optional data contract
    surfaces { "chat"; "dashboard"; "app-page" }
  }
}
```
For `kind = "sandboxed"`, `entry` is the TSX/JSX source and the optional `spec`
is a contract JSON declaring `inputs`, named data `bindings` (semantic/sql),
`outputs`, and optionally `title` — never `bundle` or `view` (the platform fills
`bundle.ref` from the compiled entry). Actions are not bindings: sandboxed
source invokes its own application's actions through the governed
`window.tangram.performAction({ resourceType; action }, args)` SDK call.
Artifact paths in `ui/spec.pkl` are relative to the containing `manifests/ui/`
directory, and a sandboxed component's entry must live in its own
`components/<component-name>/` directory.

`./gradlew evalUiComponentsExample` renders the example the way the platform loader does.

# Releasing

Releases are cut by pushing a tag named `tangram-app-manifest@<version>`;
CI (`.github/workflows/release.yaml`) runs `./gradlew createPackage` and
uploads the package to the matching GitHub release. Before tagging:

1. `./gradlew verifyContractFixture evalUiComponentsExample evalOAuthConnectorExample`
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
