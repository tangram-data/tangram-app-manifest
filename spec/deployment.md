# Deployment

**Spec revision:** 1.0 (draft)

An application may deploy workloads in two ways, freely mixed: **Tangram-managed components** (`deployment/components.pkl`) and **Helm charts** (`deployment/helm_charts.pkl`). Both run in a namespace the platform derives per install; both may consume **infrastructure claims** (`deployment/dependencies.pkl`) and the injected runtime context.

## Tangram-managed components — `components.pkl`

A component is a Kubernetes workload the platform itself plans and applies. Each component is a top-level Pkl property of type `AppComponentSpec`, annotated with `@AppComponentMeta` so the platform can discover metadata **without evaluating the full spec**:

```pkl
import "@tangram-app-manifest/deployment.pkl" as deployment
import "@tangram-app-manifest/deployment-runtime.pkl" as deploymentRuntime
import "dependencies.pkl" as deps

local db = deps.postgresDb.database()
local dbSecrets = deps.postgresDb.secrets()

@deployment.AppComponentMeta {
  name = "catalog-service"
  recommendedReplica = 3
  containers = List(new deployment.ContainerMeta {
    name = "catalog-server"
    image = "ghcr.io/tangram-data/tangram-iceberg-catalog:1.0.0"
  })
  observability = new deployment.ComponentObservability {
    traces = "otlp"
    metrics = new deployment.OtlpMetrics {}
  }
}
catalogService: deployment.AppComponentSpec = new deployment.AppComponentSpec {
  service = new deployment.AppComponentServiceSpec { port = 9494; targetPort = 9494; protocol = "TCP" }
  containers = List(new deployment.AppContainerSpec {
    ports = List(9494)
    variables = Map(
      "JDBC_URL", "jdbc:postgresql://\(db.host):\(db.port)/\(db.name)",
      "JDBC_PASSWORD", new deployment.SecretValue { value = dbSecrets.password }
    )
  })
}
```

The annotation MUST be the `AppComponentMeta` class from the schema package's deployment module — a structurally identical package-local class MUST be rejected.

### `AppComponentMeta`

| Field | Type | Meaning |
|---|---|---|
| `name` | `String` (DNS-1035 label) | Component name — also its Kubernetes Service name |
| `containers` / `initContainers` | `List<ContainerMeta>` | Image inventory (`name`, `image` as `repository[:tag]` or `repository@sha256:…`, `description?`). Pin a digest when the reference must be immutable. |
| `sourceArtifact` | `SourceArtifact?` | When present, the platform **builds** the primary container from packaged source (below); `containers` then describe prebuilt sidecars |
| `recommendedReplica` / `maxReplica` | `Int?` | Replica defaults and ceiling (default recommended: 1) |
| `optional` | `Boolean?` | Whether installs may disable the component |
| `observability` | `ComponentObservability?` | Telemetry collection (below) |

### `AppComponentSpec`

| Field | Type | Meaning |
|---|---|---|
| `service` | `AppComponentServiceSpec?` | `port`, `targetPort`, `protocol` — the Service in front of the component |
| `containers` / `initContainers` | `List<AppContainerSpec>` | `ports`, `variables` (`Map<String, String \| SecretValue>` — wrap sensitive values in `SecretValue` so the platform materializes them as Kubernetes Secrets, not plain env), `files` (`ContainerFile`: `name`, `path`, `content`, `isSensitive?`), `command`, `args?` |

### Source-built components — `SourceArtifact`

Instead of a prebuilt image, a component may ship source that the platform builds with an immutable, platform-controlled recipe:

| Field | Type | Meaning |
|---|---|---|
| `containerName` | `String` | Name of the synthesized primary container (position zero) |
| `runtime` | `SourceRuntime` | Closed set; v1 defines `python-3.12`. Each identifier maps to a platform builder/base-image recipe — adding a value is a platform capability release. |
| `entry` | `String` | Runtime-interpreted entry; for `python-3.12`, a dotted module under the source `src/` tree launched as `python -m <entry>` |
| `lockfile` | `String` | Dependency lockfile relative to the source dir; `python-3.12` REQUIRES a `uv.lock` |
| `sourceDir` | `String?` | Source tree location relative to `deployment/` (default `deployment/source/<component-name>/`) |

Every `SourceArtifact` field MUST be a schema-time constant literal — no settings, secrets, dependency outputs, or deployment context.

## Helm charts — `helm_charts.pkl`

The file MUST expose a `charts` expression evaluating to `List<HelmChart>`:

| Field | Type | Meaning |
|---|---|---|
| `repo`, `fullName`, `version` | `String` | Chart coordinates |
| `requestedCapabilities` | `HelmRequestedCapabilities` | Static request for high-impact abilities (below) |
| `components` | `List<HelmComponent>` | Per-workload metadata mirroring `AppComponentMeta` (name, replicas, optional, container inventory) |
| `observability` | `ComponentObservability?` | Telemetry collection |
| `crds` | `List<CustomResourceRbac>?` | RBAC needed for custom resources the chart manages (`apiGroup`, `resources`, `verbs`) |

### Capability requests

`HelmRequestedCapabilities` — `hooks: Boolean` (default false), `crds: Boolean` (default false), `clusterScopedKinds: Set<String>` (default empty) — is a **request, not permission**: registration records it, install preflight compares it against target policy, the approval artifact shows it, and rendered output may not exceed it. An unrequested hook, CRD, or cluster-scoped kind in the rendered chart is a **hard planning failure**.

## Infrastructure claims — `dependencies.pkl`

Applications claim platform-provisioned shared infrastructure declaratively. v1 defines two claim classes:

| Claim class | Resource type | Provisioned value / secrets |
|---|---|---|
| `PostgresqlDatabaseClaim` | `ai.tangram.os.PostgresqlDatabase` | `database()` → host/port/name; `secrets()` → user/password |
| `StorageBucketClaim` | `ai.tangram.os.StorageBucket` | `bucket()` → type/name/region/endpoint; `accessPermit()` → access/secret key |

```pkl
import "@tangram-app-manifest/infra.pkl" as infra

postgresDb: infra.PostgresqlDatabaseClaim = new { resourceName = "catalog-db" }
storageBucketClaim: infra.StorageBucketClaim = new { resourceName = "warehouse" }
```

Rules a conforming platform enforces:

- `deployment/dependencies.pkl` is the **only** claims file; a top-level property that is not a claim is rejected.
- The claim MUST be one of the exact schema-package classes — lookalike local classes are rejected, and an instance overriding `resourceType` away from the canonical value is rejected. The class, not the authored string, decides the resource type.
- Claim fields MUST be schema constants (no injected context in claim declarations).

At deployment evaluation, each provisioned claim is injected as the external property `tangram_os_ir_<resourceName>` (JSON `{resource, secrets}`); the claim classes' accessor functions decode it.

## Injected runtime context

`deployment-runtime.pkl` exposes the platform-injected evaluation context (see [Package Layout](package.md#injected-context)):

- `context` — a typed `DeploymentContext`: `id`, `tangramOSPublicUrl`, `workspace`, `k8sNamespace`, `cloudProvider`, and `serviceAccountTokenSecret` (the platform-issued Kubernetes Secret carrying the app's service-account token, referenced from Helm values or env instead of deriving Secret names from conventions).
- `getConfig(key)` / `getOptionalConfig(key)` — install-time settings.
- `getSecret(key)` / `getOptionalSecret(key)` — install-time secrets.
- `getComponentReplicas(component)`, `getComponentContainerK8sCpuQuantity(component, container)`, `getComponentContainerK8sMemoryQuantity(component, container)` — operator-resolved sizing, formatted for Kubernetes.
- `getImagePullSecretName()` — optional registry pull secret.

## Observability

`ComponentObservability` declares telemetry collection per component or chart:

- `traces` — `"otlp"` injects the platform OTLP trace endpoint into target containers; `"none"` (default).
- `metrics` — discriminated on `kind`: `OtlpMetrics` (push; inject the platform OTLP metrics endpoint) or `PrometheusMetrics` (pull; scrape `port`/`path`/`scheme` at `interval`, optional `sampleLimit`, converted to OTLP).
- `targetContainers` — defaults to all non-init containers.

Manifests never name a telemetry index or workspace: the platform attributes every signal to the workspace from the namespace label it controls.

## Integrations — `integrations/*.pkl`

An application may implement platform integration interfaces — typed contracts owned by another application. Each file declares an `IntegrationImplementation`: the target `AppIntegrationInterface` (`app`, `name`, `version`), an `implementationSpec` (interface-defined shape), and optional `implementationType`/`version`. Well-known interfaces in v1: `ai.tangram-os/data-catalog` `spark-io` and `schema-update-executor`, and `ai.tangram-os/sql` `queryable` (whose spec maps SQL command classes to required privileges).
