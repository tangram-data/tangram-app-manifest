# Tangram App Manifest

## Tangram CLI

See the [Tangram CLI installation guide](TANGRAM_CLI.md) for usage, platform
requirements, checksums, and source information.

- [Download the native macOS Apple Silicon distribution](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0-macos-aarch64.tar.gz)
- [Download the cross-platform JAR](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0.jar) (requires JDK 21)
- [View the Tangram CLI 1.0.0 release](https://github.com/tangram-data/tangram-app-manifest/releases/tag/tangram-cli%401.0.0)

## Example usage

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

## Installing the app-builder skill (Claude, Codex, others)

The repo ships two agent-facing skills: `tangram-app-builder` (build an
app with the Python SDK: package layout, typed Pkl templates, the
validate/run/call loop, and the gotchas) and `tangram-connector-builder`
(build a connector to an external SaaS API — Gmail/Slack style — with
platform-managed OAuth). Canonical skills, three install lanes
(`/plugin install tangram-app-builder` ships both; via the SDK use
`tangram-app skill install <name>` with any scope below):

**Claude Code — plugin (no SDK needed):**

```text
/plugin marketplace add tangram-data/tangram-app-manifest
/plugin install tangram-app-builder
```

**Claude Code — via the SDK:**

```sh
pip install tangram-app-sdk            # until PyPI: pip install ./python-sdk from a checkout
tangram-app skill install-builder --project .   # this repo only  → ./.claude/skills/
tangram-app skill install-builder --user        # all repos       → ~/.claude/skills/
```

Claude Code discovers the skill automatically; inside this repository it is
already active with no installation at all.

**Codex (no Claude installation assumed):**

```sh
pip install tangram-app-sdk
tangram-app skill install-builder --codex       # → ~/.codex/prompts/tangram-app-builder.md
```

This makes it invocable as the `/tangram-app-builder` custom prompt. For
automatic use, add to `~/.codex/AGENTS.md`:

```markdown
When asked to create, modify, or debug a Tangram app, first read
~/.codex/prompts/tangram-app-builder.md and follow it.
```

**Any other agent:** the skill is plain markdown with no harness-specific
content — install with any lane above (or copy
`skills/tangram-app-builder/SKILL.md` from this repo) and point your
agent's instructions file at it.

Skill prerequisites (the Pkl CLI, Python, optional PostgreSQL) are listed
inside the skill itself; agents check before installing. All copies are
sha-pinned to the packaged one by `python-sdk/tests/test_builder_skill.py`.

## OAuth-backed connectors

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

## Publishing UI components
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

## Releasing

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

## Reference
[pkl-lang: Package Import](https://pkl-lang.org/main/current/language-reference/index.html#import-clause)
