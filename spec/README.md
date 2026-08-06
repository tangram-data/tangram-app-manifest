# Tangram App Manifest Specification

**Spec revision:** 1.0 (draft) · **Manifest spec version:** `v1` · **Schema package:** `tangram-app-manifest@1.0.0`

The Tangram App Manifest is an open specification for describing applications that run on a Tangram OS platform — what an application *is* (identity and metadata), what it *exposes* (resources, actions, APIs, UI components, agent tools), what it *needs* (configuration, secrets, infrastructure), and how it is *deployed* (containers, source builds, Helm charts).

A manifest is a declarative package. It contains no imperative install logic: the platform reads the manifest and derives registration, authorization, deployment, and UI behavior from it. This is the same relationship the [Model Context Protocol](https://modelcontextprotocol.io) has to its TypeScript schema — the published schema package is the single source of truth, and this document is its normative prose companion.

## Specification pages

| Page | Contents |
|---|---|
| [Overview](overview.md) | Design principles, terminology, conformance, notation |
| [Package Layout](package.md) | The manifest package structure and evaluation model |
| [Application](application.md) | Identity and metadata (`app.pkl`), settings and secrets |
| [Resources & Actions](resources.md) | The resource model: types, actions, privileges, effects, roles |
| [App APIs](api.md) | API backend binding and the OpenAPI contract |
| [Connectors](connector.md) | Upstream endpoints, request authentication, OAuth 2.0 |
| [Agents](agent.md) | Agent identity, tool declarations, bindings, skills |
| [UI](ui.md) | Published UI components, surfaces, and UI deployment modes |
| [Deployment](deployment.md) | Components, source artifacts, Helm, infrastructure claims, observability, integrations |
| [Versioning](versioning.md) | Schema versioning and compatibility policy |

## Schema

The canonical, machine-readable schema is the published [Pkl](https://pkl-lang.org) package:

```
package://pkg.pkl-lang.org/github.com/tangram-data/tangram-app-manifest/tangram-app-manifest@1.0.0
```

Manifests are authored in Pkl and evaluated by the platform to a JSON projection. Where this prose and the published schema disagree, the schema wins.

## Status of this document

This is a draft for public review. Normative requirements use RFC 2119 keywords (**MUST**, **SHOULD**, **MAY**) as described in the [Overview](overview.md#conformance).
