# Versioning

**Spec revision:** 1.0

Three version numbers are in play, with distinct roles:

| Version | Declared where | Governs |
|---|---|---|
| **Manifest spec version** (`"v1"`) | `app.pkl` → `manifestSpecVersion` | The manifest *vocabulary* a package targets. Platforms advertise the spec versions they accept. |
| **Schema package version** (`tangram-app-manifest@1.0.0`) | `PklProject` dependency pin | The exact authoring schema. Semantic versioning; consumers pin exact versions and record checksums in `PklProject.deps.json`. |
| **Application version** (`0.1.0`, …) | `app.pkl` → `version` | The application package itself; drives catalog updates and upgrade flows. |

## Compatibility rules for the schema package

Because the JSON projection is the wire contract ([Package Layout](package.md#evaluation-model)), the following are **breaking changes** and REQUIRE a major version bump:

- Renaming or removing a serialized field.
- Changing a discriminator value (`kind`/`type` literals).
- Removing or renaming a schema module.
- Narrowing a type or constraint such that previously valid manifests fail.

Additive changes — new optional fields, new discriminated-union variants, new modules, new constants — are minor versions. Purely internal changes (docs, constraint error messages) are patches.

A contract fixture (a manifest exercising the full vocabulary, rendered to JSON and snapshot-diffed) guards these rules in the schema repository; intentional drift is regenerated and reviewed in the release commit.

## Forward declarations

The schema deliberately admits some values the current runtime rejects (e.g. `PrivateKeyJwt` token auth, `NamedTenant` connection scope, the `UserInfo`/`RequiredAtInstall` tenant sources, the reserved `scopePrivilegePropagation` field). This lets authors declare intent ahead of runtime support. A conforming platform MUST reject such declarations **at install or first use with a clear "not yet supported" error**, never by failing silently or misbehaving later. This spec marks each such value where it is defined.

## Platform obligations

- A platform MUST reject a manifest whose `manifestSpecVersion` it does not support, naming the versions it does.
- A platform MUST reject removed vocabulary with a pointer to the replacement (e.g. `appType = "ConnectorApp"` → `"Connector"`), not a generic parse error.
- A platform SHOULD evaluate manifests against the exact schema package version the manifest pins, so author-side and platform-side validation agree.

## Application upgrades

Application `version` changes flow through the platform catalog. Authors SHOULD treat these as breaking for installed workspaces and document migration: removing a resource type or action other principals may hold roles on; renaming a component (its Service name changes); changing an OAuth `connectionScope` to exclude a mode with live connections; removing a declared setting or secret that deployment expressions read.

## Spec revisions

This prose specification is revised alongside the schema package. A spec revision that only clarifies prose does not change `manifestSpecVersion`; a revision that changes the vocabulary ships with a new schema major version, and — if platforms cannot accept old and new packages simultaneously — a new `manifestSpecVersion`.
