# Agents

**Spec revision:** 1.0 (draft)

An `Agent` application is an instruction/model/tool-driven runtime. Its manifest declares the agent's identity (default LLM, system prompt), the tools it can invoke, and the skills it can activate. A key design point: the manifest declares tool **intent**; the concrete **binding** (which backend a tool resolves to) may default in the manifest but is typically chosen at install time.

`manifests/agent/spec.pkl` amends the schema module and sets fields at the top level:

```pkl
amends "@tangram-app-manifest/agent.pkl"

defaultLlm = new AgentLlmRef { provider = "Anthropic"; model = "claude-sonnet-4-6" }
systemPrompt = "You are the receipts agent. Match uploaded receipts to bank transactions…"
tools = new Listing {
  new AgentToolDecl {
    name = "query_entities"
    description = "Run a QuickBooks SQL-like SELECT over company data."
    requiresConfirmation = false
    requiredPrivilege = "Entity:Read"
    defaultBinding = new AppAction { app = "com.intuit/quickbooks"; action = "Entity.Query" }
  }
}
```

## Top-level fields

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `defaultLlm` | `AgentLlmRef` | SHOULD | Provider + model the agent runs under. `provider` is an open string but SHOULD name a provider the platform supports (v1: Anthropic, OpenAI, Gemini, OpenRouter, Ollama); `model` is the provider-specific identifier. |
| `systemPrompt` | `String` | MUST | Free-form system prompt: role, tools to reach for, what to refuse or escalate. |
| `allowCustomLlm` | `Boolean` | MAY (default `true`) | Whether workspace operators may switch provider/model at runtime. Set `false` for agents tightly coupled to a specific model. |
| `tools` | `Listing<AgentToolDecl>` | MAY | Declared tools (below). |
| `skills` | `Listing<AgentSkillDecl>` | MAY | Referenced workspace skills (below). |

## Tool declarations — `AgentToolDecl`

The runtime exposes each declared tool to the LLM, enforces its privilege before invoking, and routes it through whichever binding install resolved.

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | `String` (`^[a-z][a-z0-9_]*$`) | MUST | Stable identifier shown to the LLM; the dispatcher key. |
| `description` | `String` | MUST | One-line description. The LLM picks tools largely off this string — be specific. |
| `requiresConfirmation` | `Boolean?` | SHOULD | When true (the install-time default), every invocation is gated behind a user confirmation. Set `false` explicitly only for safe read-only tools. |
| `requiredPrivilege` | `String?` | SHOULD | `ResourceType:Privilege` string (e.g. `Lead:Create`) checked against the calling principal before each invocation. |
| `scope` | `String?` | MAY | Scope template for the IAM check (e.g. `workspace/{workspace}`). Defaults to the agent's own deployment. |
| `inputSchema` / `outputSchema` | `String?` | MAY | Reference to a schema fragment (e.g. `openapi://#components/schemas/CreateLeadRequest`) used for the LLM-side signature and bind-time checks on `AppAction` bindings. |
| `defaultBinding` | `AgentToolBinding?` | MAY | Fallback binding accepted silently at install unless the operator overrides. When neither a default nor an operator binding exists, install MUST fail with a clear error. |

### Tool bindings — `AgentToolBinding`

A discriminated union on `kind`:

| Kind | Fields | Semantics |
|---|---|---|
| `AppAction` | `app` (`group/name`), `action` (`ResourceType.ActionName`) | Invoke an action of another installed application. The gateway routes the call under the **caller's** identity, and the target action's own IAM declaration applies as a second check — a tool binding never escalates privilege. |
| `HttpEndpoint` | `method`, `url`, `auth?` | Invoke an external HTTP endpoint. `url` may contain `{name}` placeholders substituted from tool arguments; bodyless methods (GET/DELETE) encode arguments into path and query, body-bearing methods send JSON. `auth` names a workspace secret injected as a `Bearer` Authorization header — for any other auth shape, wrap the upstream in a [Connector](connector.md) and bind via `AppAction`. |
| `Builtin` | `name` | A platform-provided built-in tool (e.g. `tangram_query_app_db`), resolved from the platform's builtin registry at invocation time; missing builtins fail with a clear error. |

## Skill references — `AgentSkillDecl`

| Field | Type | Meaning |
|---|---|---|
| `name` | `String` | Skill name in the workspace skill registry |
| `version` | `String` | Required version |

Skills are resolved at install time against the installing workspace's registry — the skill itself is not packaged inside the manifest. Install MUST abort with a clear error if a referenced skill is missing or its version does not match.

## Runtime obligations

A conforming platform running an Agent application:

1. MUST check `requiredPrivilege` (when declared) via its IAM before every tool invocation.
2. MUST apply the confirmation gate for tools whose effective `requiresConfirmation` is true.
3. MUST route `AppAction` bindings through the same gateway path as direct API calls, so the target action's privilege, effect, and audit semantics apply unchanged.
4. SHOULD let workspace operators rebind tools at install (`--tool-binding`) without manifest changes.
