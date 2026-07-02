# Example usage:
```
import "package://github.com/tangram-data/tangram-app-manifest/releases/download/1.0.0/tangram-app-manifest@1.0.0#/core.pkl" as core

secrets = List(
  new core.ConfigField {
    name = "token"
    required = true
    description = "Databricks PAT token of a user who has admin permissions of the target databricks workspace"
  }
)
```
# Publishing UI components:
An app publishes reusable UI components by shipping `manifests/ui/components.pkl`
(amends `ui.pkl`; see `examples/ui-components/`):
```
amends "@tangram-app-manifest/ui.pkl"

components {
  new AppUIComponentSpec {
    name = "top-accounts"
    kind = "declarative"
    spec = "ui/components/top-accounts.json"
    surfaces { "chat"; "dashboard"; "app-page" }
  }
}
```
`./gradlew evalUiComponentsExample` renders the example the way the platform loader does.

# Reference:
[pkl-lang: Package Import](https://pkl-lang.org/main/current/language-reference/index.html#import-clause)