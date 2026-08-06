# Resources & Actions

**Spec revision:** 1.0 (draft)

The resource model is the heart of the manifest: it is how an application's capabilities become *governed* capabilities. An application declares the nouns it owns (**resource types**), the verbs on them (**actions**), what each verb requires (**privileges**) and does to the world (**effects**), and the named permission bundles principals can hold (**roles**). The platform derives authorization checks, AI autonomy policy, audit policy, and confirmation prompts from these declarations.

`manifests/api/resources.pkl` MUST expose a top-level expression:

```pkl
import "@tangram-app-manifest/resources.pkl" as resources

types: List<resources.ResourceTypeDefinition> = List( … )
```

## Resource types

### `ResourceTypeDefinition`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | `String` | MUST | Type name, unique within the application (e.g. `Purchase`). |
| `doc` | `String` | MUST | Human/LLM-facing description of the type. |
| `activeVersion` | `String` | MUST | The version served by default; MUST name an entry of `versions`. |
| `versions` | `List<ResourceTypeVersion>` | MUST | Declared versions. |
| `superType` | `ResourceType?` | MAY | Parent type this type inherits from (may be a platform super type). |
| `scopeType` | `ResourceType?` | MAY | The type whose instances scope instances of this type (e.g. everything is scoped by `Workspace`). |
| `scopePrivilegePropagation` | `Map<String, List<String>>?` | MAY | *Reserved.* Maps a parent-scope privilege to child privileges it grants. Not yet enforced; authors SHOULD omit it. |

### `ResourceTypeVersion`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `version` | `String` | MUST | Version label (e.g. `"v1"`). |
| `served` | `Boolean` | MUST | Whether this version is currently served. |
| `superTypeVersion` | `String?` | MAY | The supertype version actions/roles are inherited from. |
| `actions` | `List<Action>` | MUST | The actions this version declares. |
| `presetRoles` | `List<Role>?` | MAY | ReBAC roles for instances of this type (see [Roles](#roles)). |

A resource type's global identity is the pair *(owning application, type name)*. Cross-application references write it as `group/app/TypeName`; a bare `TypeName` refers to a type of the same application.

### Platform super types

The platform application `ai.tangram-os/core` owns well-known super types that manifests may extend or scope under: `Workspace`, `Dataset`, `DataCatalog`, `Storage`, `Database`, `Compute`, `SQLEngine`, `Workflow`, `Dashboard`. The schema package exports typed constants for each.

## Actions

### `Action`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | `String` | MUST | Action name, unique within the version (e.g. `Create`). |
| `effect` | `"Stateless" \| "Reversible" \| "Irreversible"` | MUST | Effect classification (below). Closed set — any other value MUST be rejected. |
| `idempotent` | `Boolean` | MUST | Whether repeating the call with the same arguments is safe. |
| `doc` | `String` | MUST | Description. This string is what AI agents read to decide when and how to call the action — write it for the model, with concrete argument guidance. |
| `privilege` | `String?` | SHOULD | Privilege class the action belongs to. Defaults to the action name when that matches a well-known privilege. |
| `skipAuth` | `Boolean?` | MAY | Skip the platform IAM check. Use only for intentionally public actions. |
| `skipAudit` | `Boolean?` | MAY | Suppress audit recording. |
| `requiresConfirmation` | `Boolean?` | MAY | Force a user confirmation prompt before AI-initiated invocation. |
| `additionalPrivileges` | `List<ResourcePrivilegeRequirement>?` | MAY | Cross-resource authorization the action also requires. |
| `openApiMapping` | `OpenApiMapping?` | MAY | HTTP binding for a single `operationId`. |
| `openApiMappings` | `List<OpenApiMapping>?` | MAY | Multiple HTTP bindings (e.g. native API + platform wrapper API). |
| `presentation` | `ActionPresentation?` | MAY | Typed presentation of the action's result (below). |

### Privileges

Privileges are **open strings** — an application may define domain-specific privileges (`Approve`, `Publish`). The well-known constants `Describe`, `Read`, `Write`, `Create`, `Delete`, `Admin`, `Execute` are provided for convenience and are what built-in platform policy reasons about. Authors SHOULD map actions onto well-known privileges when the semantics fit.

### Effects

The effect classifies what an action does to the world and drives AI autonomy, audit, and UI affordances:

| Effect | Semantics | Platform behavior |
|---|---|---|
| `Stateless` | Reads only; no state change | AI may execute autonomously; audit skipped by default |
| `Reversible` | Mutates state; prior state restorable | Audit always recorded |
| `Irreversible` | Mutates state; cannot be undone | AI requires confirmation; audit always recorded |

Authors MUST classify honestly: declaring a mutating action `Stateless` is a conformance violation, not an optimization.

### Cross-resource requirements

`ResourcePrivilegeRequirement` declares that an action *also* requires a privilege on another resource, named by extractors over the same request:

| Field | Type | Meaning |
|---|---|---|
| `resourceType` | `String` | `group/app/TypeName`, or bare `TypeName` for the same application |
| `privilege` | `String` | Required privilege on that resource |
| `resourceName` | `List<ResourceNameExtractor>` | Where the resource's name comes from in the request |

## Binding actions to HTTP — `OpenApiMapping`

Each mapping binds the action to one `operationId` in the package's OpenAPI document (see [App APIs](api.md)).

| Field | Type | Meaning |
|---|---|---|
| `operationId` | `String` | The bound OpenAPI operation. MUST exist in `open_api.yml`. |
| `resourceName` | `List<ResourceNameExtractor>?` | Segments forming the resource name. When omitted, the platform derives it from path parameters in URL order (or resolves it via `resourceId`). |
| `resourceId` | `ResourceNameExtractor?` | Opaque server-assigned identifier, resolved against the platform's resource registry (for APIs that identify by id rather than name). |
| `newName` | `ResourceNameExtractor?` | For Rename/Update actions: where the new name comes from. |
| `resourceNameTemplate` / `resourceIdTemplate` / `newNameTemplate` | `String?` | Escape hatches — raw template expressions for cases the typed extractors cannot express (id lookups, deferred evaluation, defaults). A template takes precedence over its typed counterpart. |

### Name extractors

Extractors are a discriminated union (`type` field) identifying where a value comes from in the HTTP exchange:

| Variant | Fields | Source |
|---|---|---|
| `PathParam` | `name` | URL path parameter (`in: path`) |
| `QueryParam` | `name` | Query-string parameter (`in: query`) — distinct from `PathParam` because OpenAPI parameter identity is the pair *(name, in)* |
| `BodyField` | `path` | Request-body field, dot-separated with inline array indices (`obj.items[0].name`) |
| `ResponseField` | `path` | Response-body field — for server-assigned identifiers only known after the upstream responds |

## Roles

A `Role` names a bundle of privileges that a principal can hold **per resource instance** under the platform's relationship-based access control (ReBAC) model. At check time the platform resolves the role's `permissions` against the requested privilege.

| Field | Type | Meaning |
|---|---|---|
| `name` | `String` | Role name, unique within the carrying version. Used as the grant's role value. |
| `permissions` | `List<String>` | Privileges the role grants. Each MUST be a declared action/privilege of the type, or the wildcard `"*"`. |
| `description` | `String` | Shown in admin UIs. |

Inheritance and shadowing rules:

- Roles inherit from the supertype version, if any.
- A role with the same name as an inherited role **replaces** (shadows) it entirely — there is no permission union.
- `"*"` expands at registration time to the type's full action set, including inherited actions. Use sparingly: a wildcard role silently grants every future action added to the type.

## Result presentation

An action MAY declare how its result renders in conversational and app surfaces. `ActionPresentation` is a discriminated union on `kind`:

| Kind | Purpose | Fields |
|---|---|---|
| `card` | Read-only result card | `title?`, `fields?` |
| `picker` | Selection from result entries | `title?`, `fields?` |
| `confirm` | Confirmation summary | `title?`, `fields?` |
| `form` | Editable form over the result | `title?`, `fields?` |
| `component` | Render a published UI component of this application | `component`, `preferredDisplayMode`, `inputs` |

For `component`: `component` is the manifest-local component name (resolved within this application); `preferredDisplayMode` is `"panel"` (default) or `"inline"` and is a *preference* the host may override; `inputs` maps component input names to sources. Sources are deliberately selection-only — no templates, functions, or transforms:

- `{ source: "arg", path: [...] }` — a path into the action's LLM-facing argument object.
- `{ source: "literal", value: … }` — a constant.

## Example

```pkl
new resources.ResourceTypeDefinition {
  name = "Purchase"
  doc = "An expense transaction — money leaving a bank or credit-card account."
  activeVersion = "v1"
  versions = List(new resources.ResourceTypeVersion {
    version = "v1"
    served = true
    actions = List(
      new resources.Action {
        name = "Create"
        privilege = "Create"
        effect = "Reversible"
        idempotent = false
        requiresConfirmation = true
        doc = "Record a new expense against a bank or credit-card account. Writes to the ledger — confirmation-gated."
        openApiMapping = new resources.OpenApiMapping { operationId = "createPurchase" }
      }
    )
    presetRoles = List(new resources.Role {
      name = "user"
      permissions = List("Create")
      description = "Record expenses through the connector."
    })
  })
}
```
