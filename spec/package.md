# Package Layout

**Spec revision:** 1.0

A manifest package is a directory tree rooted at `manifests/`. It is typically distributed as a `.tar.gz`, `.tar`, or `.zip` archive, or read directly from a repository checkout.

## Directory structure

```
manifests/
├── PklProject                 # Pkl project file pinning the schema package
├── PklProject.deps.json       # Resolved dependency checksums
├── app.pkl                    # REQUIRED — application identity & metadata
├── README.md                  # Long-form description (referenced from app.pkl)
├── settings.pkl               # OPTIONAL — install-time settings declaration
├── secrets.pkl                # OPTIONAL — install-time secrets declaration
├── api/
│   ├── spec.pkl               # API binding (App form or Connector form)
│   ├── open_api.yml           # OpenAPI 3 document
│   └── resources.pkl          # Resource types, actions, roles
├── agent/
│   └── spec.pkl               # Agent declaration (Agent apps)
├── ui/
│   ├── spec.pkl               # Published components + UI deployment mode
│   ├── components/<name>/…    # Component sources/artifacts (one dir per component)
│   └── static/…               # Logos and static assets
├── deployment/
│   ├── components.pkl         # Tangram-managed Kubernetes components
│   ├── dependencies.pkl       # Shared infrastructure claims
│   ├── helm_charts.pkl        # Helm chart declarations
│   └── source/<component>/…   # Source trees for source-built components
└── integrations/
    └── *.pkl                  # Platform integration implementations
```

Only `app.pkl` is universally REQUIRED. Every other file is required conditionally by what the application declares:

- An application that exposes an API MUST provide `api/spec.pkl`, `api/open_api.yml`, and `api/resources.pkl`.
- An `Agent` application MUST provide `agent/spec.pkl`.
- An application that deploys Tangram-managed components MUST provide `deployment/components.pkl`; one that deploys Helm charts MUST provide `deployment/helm_charts.pkl`.
- An application that claims shared infrastructure MUST declare it in `deployment/dependencies.pkl` — this is the only claims file; platforms MUST NOT honor claims declared elsewhere.

## The Pkl project

`manifests/PklProject` MUST declare a dependency on the schema package, pinned to an exact version:

```pkl
dependencies {
  ["tangram-app-manifest"] {
    uri = "package://pkg.pkl-lang.org/github.com/tangram-data/tangram-app-manifest/tangram-app-manifest@1.0.0"
  }
}
```

Manifest modules then import schema modules through the dependency alias:

```pkl
import "@tangram-app-manifest/resources.pkl" as resources
```

`PklProject.deps.json` records the resolved checksum of the pinned package and SHOULD be committed alongside the manifest so evaluation is reproducible.

## Evaluation model

Platforms evaluate manifest modules with a Pkl evaluator configured from the package's `PklProject` (so dependency-alias imports resolve), render the result to JSON, and decode the JSON projection. Consequences:

1. **The JSON projection is the wire contract.** Renaming a serialized field, changing a discriminator value, or removing a module is a breaking change (see [Versioning](versioning.md)).
2. **Discriminators are explicit.** Pkl class identity does not survive JSON rendering, so every polymorphic hierarchy in the schema (tool bindings, tenant sources, name extractors, connection scopes, presentations, metrics collection) carries a `kind` or `type` discriminator field that the platform decoder matches on. In several hierarchies the concrete classes pin the discriminator with a singleton literal type; in the others it is a defaulted string. Either way, authors MUST NOT override a discriminator value — doing so mis-routes the decoder.
3. **Expression entry points.** Some files are evaluated for a single named expression rather than their whole output: `api/resources.pkl` for `types`, `settings.pkl` for `settings`, `secrets.pkl` for `secrets`, `deployment/helm_charts.pkl` for `charts`. These entry-point names (and the top-level field shapes of `app.pkl` and the `spec.pkl` files) are defined by this specification and evaluated by the platform — they are not classes in the schema package.
4. **Annotations are schema-qualified.** Deployment component metadata is discovered via the `@AppComponentMeta` Pkl annotation, which MUST be the class declared in the schema package's deployment module — a structurally identical local class is rejected. The same exact-class rule applies to shared infrastructure claims.

## Injected context

Deployment expressions MAY read platform-injected external properties (and only those). The schema package's `deployment-runtime.pkl` module wraps them:

| Property | Meaning |
|---|---|
| `prop:tangram_app_deployment_id` | Unique deployment id |
| `prop:tangram_os_public_url` | The platform's public URL |
| `prop:tangram_app_deployment_workspace` | Installing workspace |
| `prop:tangram_app_deployment_k8s_namespace` | Derived install namespace |
| `prop:tangram_os_cloud_provider` | One of `Local`, `AWS`, `Azure`, `GCP`, `Scaleway`, `Ovh` |
| `prop:tangram_app_sa_token_secret_name` / `_key` | Platform-issued service-account token Secret binding |
| `prop:deploymentConfig` | Install-time settings, secrets, replica counts, and container resource requests (JSON) |
| `prop:tangram_os_ir_<resourceName>` | Each provisioned infrastructure claim's resource + secrets (JSON) |

Properties are injected lazily: a property is only required when a manifest expression actually references it. Manifests MUST NOT rely on the working directory, environment variables, network access, or filesystem layout at evaluation time.

## Path hygiene

Artifact paths inside the package (UI component specs and entries, deployment source directories) are relative to their owning manifest directory and MUST stay inside it. Platforms MUST reject absolute paths and `..` traversal at indexing time.
