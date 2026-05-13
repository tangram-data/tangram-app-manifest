# examples

Runnable reference manifests exercising the types in `src/`. Each example is a
standalone Pkl project that evaluates against the in-repo source via relative
imports, so you can run it without first publishing the package.

| Example                                | Demonstrates                                                                                          |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| [`weather-agent`](weather-agent/)      | AgentApp with three `HttpEndpoint` tools bound to real, no-auth public APIs — deployable as-is.       |

## Evaluating an example

From the package root:

```bash
pkl eval examples/weather-agent/manifests/app.pkl
pkl eval examples/weather-agent/manifests/agent/spec.pkl
pkl eval examples/weather-agent/manifests/api/resource_types.pkl
```

A clean exit and human-readable Pkl output means the example parses against
the local schema. Real validation (against the platform's `ManifestValidator`)
runs server-side as `tangram app manifest validate`.

## Converting an example to a real manifest

The relative imports (`../../../src/...`) are a convenience for evaluating
against this repo's source. Production manifests written by app authors should
instead reference the published package:

```pkl
amends "@tangram-app-manifest/agent.pkl"   // was: ../../../../src/agent.pkl
import "@tangram-app-manifest/core.pkl" as tangram   // was: ../../../src/core.pkl
```

The schema is identical — only the import URI changes.
