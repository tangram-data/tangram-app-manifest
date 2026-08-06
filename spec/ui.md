# UI

**Spec revision:** 1.0

`manifests/ui/spec.pkl` declares two things: the reusable **UI components** the application publishes to the platform catalog, and the **UI deployment mode** — how the application's own UI (if any) is served.

## Published components

```pkl
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
    entry = "components/churn-explorer/churn-explorer.tsx"
    spec  = "components/churn-explorer/churn-explorer.contract.json"
    surfaces { "chat"; "dashboard" }
  }
}
```

Each component is catalogued under the namespaced name `{group}/{app}/{name}` — no cross-application collisions.

### Authoring tiers

| Kind | Trust model | Artifacts |
|---|---|---|
| `declarative` | A JSON view specification rendered by the platform's trusted frontend renderer. No author code runs. | `spec` — path to the component JSON artifact. `entry` MUST be absent. |
| `sandboxed` | Author-supplied TSX/JSX compiled to a bundle at publish and executed in a sandboxed iframe with a governed SDK. | `entry` — the source entry file. `spec` — OPTIONAL render contract (inline typed value or path to contract JSON). |

### `AppUIComponentSpec`

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | `String` (lowercase slug: `[a-z0-9]([-a-z0-9]*[a-z0-9])?`) | MUST | Catalog name and the component's source directory name. |
| `kind` | `"declarative" \| "sandboxed"` | MUST | Authoring tier. |
| `description` | `String?` | SHOULD | One-line summary of what the component shows. Drives agent discovery (intent matching); fallback when the contract carries no description. |
| `surfaces` | `Listing<UIComponentSurface>` | MUST | Allowed render surfaces (below). |
| `spec` | `String \| UIComponentContract` | per kind | Declarative: the view artifact (REQUIRED). Sandboxed: the render contract (OPTIONAL). |
| `entry` | `String?` | sandboxed only | Source entry, MUST be `components/<name>/<file>` where the directory equals the component's `name`; only that directory contributes to the bundle and content hash. |

Artifact paths are relative to `manifests/ui/` and MUST stay inside it; absolute paths and `..` traversal are rejected at indexing time.

### Surfaces

`surfaces` is an **allowlist** — the runtime MUST refuse to render a component on an undeclared surface:

| Surface | Where |
|---|---|
| `chat` | Inside an AI conversation, inline or in the chat side panel |
| `dashboard` | As a reusable tile in a persisted dashboard grid |
| `app-page` | On a page within an application screen or workflow |

Declaring multiple surfaces does not adapt layout automatically; authors SHOULD verify the component suits every declared host.

### The sandboxed render contract — `UIComponentContract`

Declares what the in-frame SDK may do and what the component consumes/emits. It MUST NOT declare `bundle` or `view` — the platform fills the bundle reference from the compiled `entry`.

| Field | Type | Meaning |
|---|---|---|
| `title` | `String?` | Human-readable title; overrides the name in the catalog |
| `description` | `String?` | Summary; takes precedence over the manifest entry's `description` |
| `inputs` | JSON Schema object, OPTIONAL | Render input parameters |
| `bindings` | `Mapping<String, SemanticBinding \| SqlBinding>?` | Named data bindings callable from the in-frame SDK |
| `outputs` | `Listing<UIComponentOutput>?` | Events the component may emit (`name` + JSON-Schema `payload`) |

Data bindings:

- **`SemanticBinding`** (`type = "semantic"`, preferred) — `semanticModels` + `semanticQuery` against the platform's governed, dialect-portable semantic layer. The query may carry `{{…}}` templates resolved server-side at render.
- **`SqlBinding`** (`type = "sql"`, escape hatch) — parameterized `sql` bound to a `sqlEngineId`. `params` values bind as SQL parameters at the facade and are never string-concatenated.

**Actions are not bindings.** Sandboxed source invokes its own application's actions through the governed in-frame SDK call `window.tangram.performAction({ resourceType; action }, args)`; the gateway remains authoritative for action existence, argument validation, IAM, and confirmation. Named action bindings survive only inside declarative component JSON as view wiring.

## UI deployment mode

The `deployment` declaration selects how the application's UI is served:

```pkl
deployment: ui.UIDeployment = new { mode = "UIComponent"; rootComponent = "home" }
```

| Mode | Meaning | Extra fields |
|---|---|---|
| `None` | The application has no UI (default). | — |
| `Proxy` | The application serves its own UI from its container; the gateway forwards matching paths. | `uriPatterns` (REQUIRED), `keepAppStaticPathPrefix?`, `apiKeepAppStaticPathPrefix?` |
| `OSBuiltIn` | UI hand-built into the platform frontend. Reserved for first-party applications. | — |
| `UIComponent` | The platform renders the application's packaged components; `rootComponent` names the entry component in `components` — the root component *is* the application's UI. | `rootComponent` |

Constraints (enforced at manifest load): `uriPatterns` is meaningful only when `mode == "Proxy"`, `rootComponent` only when `mode == "UIComponent"`. `Proxy` is the only gateway-served mode and MUST declare `uriPatterns`. In `UIComponent` mode, authors SHOULD set `rootComponent`; when set, it MUST name a component declared in `components`.

## Branding assets

`ui/spec.pkl` MAY also declare top-level branding fields consumed by the platform catalog:

- `logo` — path to the application's logo image.
- `resourceTypeIcons` — map from resource type name to an icon image path.

Asset paths MUST begin with `static/` and resolve to files under `manifests/ui/static/` (e.g. `logo = "static/logo.png"`).
