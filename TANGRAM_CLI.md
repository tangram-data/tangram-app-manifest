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

## Release and source information

- [Tangram CLI 1.0.0 release](https://github.com/tangram-data/tangram-app-manifest/releases/tag/tangram-cli%401.0.0)
- [Native macOS archive checksum](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0-macos-aarch64.tar.gz.sha256)
- [Cross-platform JAR checksum](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/tangram-cli-1.0.0.jar.sha256)
- [Source and license pointers](https://github.com/tangram-data/tangram-app-manifest/releases/download/tangram-cli%401.0.0/SOURCE.md)

The native executable was built from Tangram commit `f46b7f35d` with GraalVM
Community Edition 23.0.2+7.1 and open-source Graal/Truffle 24.1.2 components.
It does not use an Oracle GraalVM binary distribution or an enterprise Truffle
component.
