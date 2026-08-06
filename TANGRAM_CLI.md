# Tangram CLI

Tangram CLI 1.0.0 is available as a native executable for Apple Silicon macOS
and as a cross-platform executable JAR.

## Apple Silicon macOS

The native distribution does not require Java at runtime. It supports Macs
reported as `arm64` by `uname -m`.

```sh
version="1.0.0"
release_url="https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%40${version}"
archive="tangram-cli-${version}-macos-aarch64.tar.gz"

curl --fail --location --remote-name "${release_url}/${archive}"
curl --fail --location --remote-name "${release_url}/${archive}.sha256"
shasum -a 256 -c "${archive}.sha256"
tar -xzf "${archive}"
mkdir -p "$HOME/.local/bin"
install -m 0755 "tangram-cli-${version}-macos-aarch64/tangram" "$HOME/.local/bin/tangram"
tangram --help
```

The native executable is not currently code-signed or notarized. If macOS
Gatekeeper quarantines the downloaded archive, inspect it before deciding
whether to remove the quarantine attribute.

## JAR for Intel macOS, Linux, and Windows

The executable JAR requires JDK 21. Download it from the release and run it with
`java`:

```sh
version="1.0.0"
release_url="https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%40${version}"
jar="tangram-cli-${version}.jar"

curl --fail --location --remote-name "${release_url}/${jar}"
curl --fail --location --remote-name "${release_url}/${jar}.sha256"
shasum -a 256 -c "${jar}.sha256"
java -jar "${jar}" --help
```

PowerShell users can run the downloaded JAR with the same Java command:

```powershell
java -jar .\tangram-cli-1.0.0.jar --help
```

## Authoring an app manifest

The CLI scaffolds and validates packages conforming to the
[Tangram App Manifest specification](spec/README.md).

### Scaffold a package

`tangram app manifest init` writes a complete skeleton — `manifests/` with
`PklProject`, `app.pkl`, `api/resources.pkl`, `settings.pkl`, `secrets.pkl`,
and the files the chosen application type needs:

```sh
# An App that deploys its own components
tangram app manifest init my-app \
  --group com.example --name my-app --version 0.1.0 --app-type App

# A Connector for a remote API (Connector form of api/spec.pkl + open_api.yml)
tangram app manifest init salesforce \
  --group com.example --name salesforce --app-type Connector

# An Agent (agent/spec.pkl with system prompt, default LLM, tool stubs)
tangram app manifest init receipts-agent \
  --group com.example --name receipts-agent --app-type Agent

# Scaffold integration plugin libs alongside (libs/<name>/<version>/ + integrations/plugins.pkl)
tangram app manifest init my-lakehouse \
  --group com.example --name my-lakehouse --lib my-spark-io:1.0.0
```

Use `--force` to overwrite files in a non-empty target directory. After
scaffolding, resolve the pinned schema package once:

```sh
cd my-app/manifests && pkl project resolve
```

### Validate locally

`tangram app manifest validate` runs the same validator the platform runs at
registration — no server connection or credentials required. It expects the
package root (the directory containing `manifests/`, and optionally `libs/`):

```sh
tangram app manifest validate my-app
```

A non-zero exit means the package would be rejected at registration; each
finding names the file and rule that failed.

## Local app preview

From a Tangram app package directory, start its Python backend and browser UI
preview without Docker:

```sh
tangram app dev .
```

Use `--no-backend` to preview only the UI, `--port 0` to select a free port, or
`--open` to open the preview automatically:

```sh
tangram app dev --no-backend --port 0 --open .
```

Run `tangram app dev --help` for all options. A backend requires Python 3.12 or
newer; use `--python <path>` to select it explicitly.

## Installing and operating apps on a platform

The remaining `app` commands talk to a Tangram OS instance; authenticate first
with `tangram login` (and `tangram use <instance>` to pick a context).

### Install, inspect, uninstall

```sh
# Install from the App Hub into a workspace
tangram app install com.intuit quickbooks --workspace my-ws --version 0.1.0 --from-app-hub

# Retry or change an existing deployment
tangram app install com.intuit quickbooks --workspace my-ws --version 0.1.0 --upgrade

# Inspect
tangram app list --workspace my-ws
tangram app get com.intuit quickbooks --workspace my-ws
tangram app component status com.intuit quickbooks catalog-service --workspace my-ws
tangram app infra-resources com.example my-app --workspace my-ws

# Uninstall
tangram app undeploy com.intuit quickbooks --workspace my-ws
```

### Agent installs: tool bindings

Agent manifests declare tool *intent*; the concrete binding is chosen at
install with repeated `--tool-binding` flags (a manifest `defaultBinding` is
used when no override is given):

```sh
tangram app install com.example receipts-agent --workspace my-ws --version 0.1.0 \
  --agent-name receipts \
  --tool-binding 'create_lead=app:com.acme/salesforce#Lead.Create' \
  --tool-binding 'lookup=http:GET:https://api.example.com/lookup/{id}#lookup-token' \
  --tool-binding 'query=builtin:tangram_query_app_db'
```

Operators can also attach extras beyond what the manifest declares:

```sh
tangram app install com.example receipts-agent --workspace my-ws --version 0.1.0 --upgrade \
  --add-tool 'ping=http:GET:https://status.example.com/ping;desc:Check upstream service health' \
  --add-skill 'expense-policy@1.2.0'
```

### Connector OAuth

For OAuth-backed connectors, drive the connection lifecycle after install:

```sh
# Prints the upstream authorize URL to open in a browser
tangram app connector oauth-start com.intuit quickbooks --workspace my-ws

# Mode, tenant, expiry, last error — token values are never returned
tangram app connector oauth-status com.intuit quickbooks --workspace my-ws

# Revokes upstream tokens (when supported) and clears local secrets
tangram app connector oauth-disconnect com.intuit quickbooks --workspace my-ws
```

### Built-app packages

`tangram app pkg` covers export, validate, and install of built-app packages
(apps produced by the platform's app builder); run `tangram app pkg --help`
for the subcommands.

## Release and source information

- [Tangram CLI 1.0.0 release](https://github.com/tangram-data/tangram-app-manifest/releases/tag/tangram-cli%401.0.0)
- [Native macOS archive checksum](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0-macos-aarch64.tar.gz.sha256)
- [Cross-platform JAR checksum](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0.jar.sha256)
- [Source and license pointers](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/SOURCE.md)

The native executable was built from Tangram commit `f46b7f35d` with GraalVM
Community Edition 23.0.2+7.1 and open-source Graal/Truffle 24.1.2 components.
It does not use an Oracle GraalVM binary distribution or an enterprise Truffle
component.
