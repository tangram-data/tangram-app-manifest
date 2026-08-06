# App APIs

**Spec revision:** 1.0 (draft)

An application that exposes an API describes it with two artifacts under `manifests/api/`:

1. **`open_api.yml`** — an OpenAPI 3 document defining operations, parameters, and schemas.
2. **`spec.pkl`** — the binding that tells the platform where those operations are served.

`spec.pkl` takes one of two forms depending on `appType`: the **App form** binds to a service the application deploys; the **Connector form** binds to a remote upstream the platform proxies ([Connectors](connector.md)). This page covers the OpenAPI contract and the App form.

## The OpenAPI contract

- The document MUST be valid OpenAPI 3.
- Every operation referenced by an action's `openApiMapping.operationId` MUST exist; each `operationId` MUST be unique.
- Operations SHOULD carry request/response schemas — the platform uses them to generate LLM-facing tool signatures and to validate arguments at the gateway.
- Client-facing paths are what workspace clients and agents call. The platform derives the public host and URL prefix from application identity and platform policy — the manifest MUST NOT author a public host or route.

### The `x-tangram-upstream` extension

For Connectors, an operation MAY carry an `x-tangram-upstream` object that rewrites the call for the upstream system. Its `path` field is a template resolved per request:

```yaml
/companyinfo:
  get:
    operationId: getCompanyInfo
    x-tangram-upstream:
      path: "/v3/company/{{oauth.tenantId}}/companyinfo/{{oauth.tenantId}}"
```

Templates may reference `{{oauth.tenantId}}` (the captured upstream tenant, see [Connectors](connector.md#tenant-capture)), `{{settings.<key>}}`, and `{{secrets.<key>}}`. This keeps client-facing paths tenant-free while the proxy substitutes tenant- and install-specific segments upstream.

## App form of `api/spec.pkl`

An `App` that serves its own API binds it to a Kubernetes Service it deploys:

```pkl
import "@tangram-app-manifest/api.pkl" as api

apiSpecFile = "open_api.yml"
backend: api.ServiceBackend = new api.ServiceBackend {
  serviceName = "catalog-service"
  port = 9494
}
```

### `ServiceBackend`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `serviceName` | `String` (DNS-1035 label) | MUST | Namespace-free name of the Kubernetes Service, inside the application's derived install namespace, that implements the API. |
| `port` | `Int` (1–65535) | MUST | The Service port (not the container target port). |

The binding is **producer-agnostic**: it does not state whether a Tangram-managed component or a Helm chart produced the Service. Validity rules:

- For a Tangram-managed component, the Service name equals the component's `AppComponentMeta.name` and the port MUST equal that component's declared `service.port`.
- For Helm, the rendered chart MUST contain exactly one namespaced Service with this name and port.

## Gateway semantics

For both forms, the platform gateway is authoritative in front of the backend. On every call it:

1. Resolves the operation and the action bound to it.
2. Validates arguments against the OpenAPI schemas.
3. Enforces the action's privilege via the platform IAM (unless `skipAuth`), including any `additionalPrivileges`.
4. Applies effect policy — confirmation for `Irreversible` (or `requiresConfirmation`) actions invoked by AI, audit per the effect class.
5. Forwards to the bound backend (deployed Service or connector upstream) with platform- or connector-injected credentials.

Backends behind the gateway SHOULD still authenticate the platform (Apps receive a platform-issued service-account token; see [Deployment](deployment.md#injected-runtime-context)) but MUST NOT re-implement workspace-user authorization — the action declarations in the manifest are the single authorization source of truth.
