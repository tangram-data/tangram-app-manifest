# weather-agent

Example AgentApp wired to three real, no-auth public APIs. Deploy this to a
Tangram OS workspace and the agent's tools will actually fire — no Salesforce
connector, no workspace secrets, no platform builtins required.

## Tools

| Tool             | Backend                                                       | Demonstrates                                          |
| ---------------- | ------------------------------------------------------------- | ----------------------------------------------------- |
| `current_weather` | `GET https://api.open-meteo.com/v1/forecast`                  | Query-string args (`latitude`, `longitude`, ...)      |
| `geocode_city`    | `GET https://geocoding-api.open-meteo.com/v1/search`          | Single query-string arg → real upstream JSON          |
| `daily_zen`       | `GET https://api.github.com/zen`                              | Zero-arg endpoint — plain text response               |

All three are `HttpEndpoint` bindings with no `auth` field, so no workspace
secret is required.

## Why no `AppAction` or `Builtin` here

- `AppAction` would require a sibling deployed Tangram app to bind against;
  this example stays self-contained.
- `Builtin` bindings have no concrete handlers yet — the first builtin
  (`tangram_query_app_db`) lands with the Database resource type.
