# Example usage:
```
import "package://github.com/tangram-data/tangram-app-manifest/releases/download/1.0.0/tangram-app-manifest@1.0.0#/tangram_os_user_config.pkl" as user_config

secrets = List(
  new user_config.ConfigField {
    name = "token"
    required = true
    description = "Databricks PAT token of a user who has admin permissions of the target databricks workspace"
  }
)
```
# Reference:
[pkl-lang: Package Import](https://pkl-lang.org/main/current/language-reference/index.html#import-clause)