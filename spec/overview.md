# Overview

**Spec revision:** 1.0 (draft)

## What a manifest is

A Tangram app manifest is a self-contained, declarative package that describes an application to a Tangram OS platform. The platform evaluates the manifest at registration and install time and derives everything else from it:

- **Identity & catalog listing** — group, name, version, description, tags.
- **Authorization model** — the resource types the app owns, the actions on them, the privilege each action requires, and the preset roles that grant those privileges.
- **API surface** — an OpenAPI 3 document bound either to a service the app deploys (Apps) or to a remote upstream the platform proxies (Connectors).
- **Agent behavior** — for Agent applications, the model, system prompt, tools, and skills.
- **UI** — published components and how the app's UI is served.
- **Deployment** — containers, source-built artifacts, Helm charts, and infrastructure claims (databases, buckets).

The manifest contains **no imperative code paths for installation**. There are no install hooks or scripts in the manifest itself; deployment expressions are pure functions of platform-injected context. This keeps installation reviewable, reproducible, and safe to evaluate.

## The three application types

Every application declares exactly one `appType`:

| Type | Meaning |
|---|---|
| `App` | Any user-facing application — whatever mix of components, Helm releases, and infrastructure claims it deploys. |
| `Connector` | Integrates a remote system. Owns upstream endpoint, request-authentication, and OAuth credential semantics; the platform proxies its API. |
| `Agent` | An instruction/model/tool-driven runtime declared in `manifests/agent/spec.pkl`. |

There is **no deployment-mode taxonomy in application identity**: an `App` may deploy nothing, one container, or a fleet of Helm charts — its type does not change. (Earlier pre-1.0 types `NativeApp`, `ConnectorApp`, and `PlatformApp` are rejected by conforming platforms with a pointer to their replacement.)

## Design principles

1. **Declarative over imperative.** The manifest states facts; the platform decides how to act on them. Anything that would require trust in author-supplied code at install time is out of scope.
2. **Governed by construction.** Every action carries a privilege and an effect classification. AI autonomy, audit, and confirmation behavior fall out of the declaration, not out of runtime convention.
3. **Typed schema, JSON wire form.** Manifests are authored in [Pkl](https://pkl-lang.org) against the published schema package and evaluated to a JSON projection. Pkl gives authors type checking, constraints, and composition; the JSON projection is what platforms decode. Polymorphic values carry explicit discriminator fields (`kind`, `type`) because JSON does not carry class identity.
4. **Capabilities are requested, not assumed.** High-impact abilities (Helm hooks, CRDs, cluster-scoped resources, OAuth scopes, endpoint overrides) are declared statically so that registration can record them, install preflight can compare them against policy, and humans can review them.
5. **Least authority for evaluation.** Manifest evaluation reads only platform-injected external properties (`prop:` reads) — never the filesystem layout, environment, or network.

## Terminology

- **Platform** — a Tangram OS deployment that registers, installs, and runs applications.
- **Author / publisher** — the party producing a manifest package.
- **Workspace** — the platform tenant an application is installed into.
- **Manifest package** — the `manifests/` directory tree (typically distributed as a `.tar.gz`/`.zip`) described in [Package Layout](package.md).
- **Resource type** — an application-owned noun (e.g. `Purchase`, `Dashboard`) that carries actions and roles; see [Resources & Actions](resources.md).
- **Action** — a named operation on a resource type, bound to an HTTP operation and governed by a privilege and an effect.
- **Schema package** — the published `tangram-app-manifest` Pkl package that defines every class in this spec.

## Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** are to be interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

Two conformance targets exist:

- A **conforming manifest** is a package that evaluates successfully against the schema package and satisfies every MUST in this specification.
- A **conforming platform** accepts every conforming manifest whose declared `manifestSpecVersion` it supports, rejects non-conforming manifests with actionable errors, and honors the declared semantics (privileges, effects, capability requests, surfaces) at runtime.

Schema-level constraints (Pkl type constraints such as regex-limited names or `https://`-only URLs) are normative. A conforming platform SHOULD additionally enforce them server-side rather than trusting the evaluator alone.

## Notation

Examples in this specification are Pkl unless noted. Field tables list the JSON projection name, its type in the schema package, and its requirement level. `Listing<T>`/`List<T>` project to JSON arrays; `Mapping<K,V>`/`Map<K,V>` to JSON objects.
