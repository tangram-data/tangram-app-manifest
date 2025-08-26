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
# Reference:
[pkl-lang: Package Import](https://pkl-lang.org/main/current/language-reference/index.html#import-clause)