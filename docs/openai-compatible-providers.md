---
title: Model Providers
description: Configure Anthropic, OpenAI-compatible, DeepSeek, and approved model providers.
---

VMA uses Deep Agents 0.6.12 with LangChain chat-model integrations. Anthropic, OpenAI, and DeepSeek are first-class adapters; operators can register additional server-approved providers.

The filename is retained for existing links, but provider support is not limited to OpenAI-compatible APIs.

Current status is **implemented for provider resolution and model construction** and **partial for behavioral portability**. A provider capability record cannot make different APIs produce identical tool calls, streaming chunks, reasoning fields, structured output, token accounting, or failure behavior.

## Security model

Agent JSON selects a provider and model ID. It cannot supply an arbitrary base
URL or raw API key. The two configuration sources are deliberately separate:

1. Model credentials come only from the Session's create-time model-Credential
   binding, created natively from a public provider ID and resolved to the
   provider's private Vault credential slot by VMA.
2. Server settings and `VMA_MODEL_PROVIDERS` supply only approved adapters,
   endpoint URLs, routing policy, model defaults, capabilities, and the private
   credential-slot name.

This prevents a tenant from turning model configuration into unrestricted server-side request forgery. Operators must still review every registered base URL, TLS policy, data-retention policy, and credential scope.

VMA never reads a model API key from process environment or provider
configuration. It constructs model objects explicitly and does not mutate
process environment or register tenant-specific Deep Agents provider profiles.
Deep Agents provider and harness registries are process-global and are unsafe
for per-request secrets.

### Session model overrides and BYOK

An Agent supplies the default model. Session creation may use CMA-compatible
`agent_with_overrides` to replace that model for one Session; the resolved model
is stored in the Session snapshot and the Agent resource is unchanged. A public
model provider or provider-prefixed model ID takes precedence over the Agent's
legacy private `runtime.model.provider` fallback.

For end-user BYOK, use the native SDK or endpoint so the caller only handles a
stable provider ID:

```python
credential = await client.vaults.model_credentials.create(
    vault_id=end_user_vault_id,
    provider="openrouter",
    api_key=end_user_api_key,
)
```

The corresponding REST endpoint is
`POST /v1/vaults/{vault_id}/model_credentials`. VMA validates the provider
against its authenticated, secret-free `model_providers` catalog and performs
the private credential-slot mapping internally. The caller never needs to know
the private slot name, such as `OPENROUTER_API_KEY`. Despite the legacy
configuration field name `api_key_env`, the slot is not read from process
environment.

Attach the Vault when creating the Session. At creation VMA reads Vaults in
request order, selects the first matching model Credential, and stores only that
Credential's ID as an immutable Session binding. For example:

```text
vault_ids = [end_user_personal_vault, customer_shared_vault]
```

This is BYOK-preferred at Session creation: a personal OpenRouter Credential wins
when present; otherwise the shared Vault supplies one. After creation VMA reloads
only the selected Credential ID. An invalid, archived, or deleted selected key
fails closed and never switches that Session to the shared Vault. A new Session
is required to choose a different payer. If no supplied Vault contains a
matching Credential at creation, VMA rejects Session creation with HTTP `422`
and stable code `model_credential_required`. There is no VMA platform key or
server-key fallback. The trusted customer backend owns the decision about
which Vaults and order to submit; VMA intentionally has no separate BYOK policy
enum.

The MVP stores one immutable model-Credential binding per Session. Therefore a
multiagent Session currently requires the coordinator and every pinned
subagent to use the same provider. A mixed-provider roster is rejected at
Session creation instead of implicitly adding more credentials.

The model Credential is decrypted only in the control plane to construct the
LangChain model client. It is never included in the public Session snapshot,
events, checkpoints, run state, or E2B sandbox. This use of a Vault credential
for inference is a VMA extension; Claude Managed Agents uses Vaults for
third-party credentials rather than selecting a non-Anthropic model provider.

## Built-in providers

### Anthropic

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=anthropic
ANTHROPIC_BASE_URL=
VMA_DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-6
```

The runtime constructs `langchain_anthropic.ChatAnthropic`. An empty `ANTHROPIC_BASE_URL` uses the provider default.

### OpenAI

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openai
OPENAI_BASE_URL=
VMA_DEFAULT_OPENAI_MODEL=gpt-5.5
OPENAI_USE_RESPONSES=false
```

The runtime constructs `langchain_openai.ChatOpenAI`. VMA defaults to Chat Completions behavior because it is the broadest compatibility mode. Set `OPENAI_USE_RESPONSES=true` only for an endpoint that implements the Responses API and after deciding its retention settings.

Deep Agents' string shorthand for `openai:*` normally enables the Responses API through a global provider profile. VMA does not use that shortcut for tenant runs; it passes a preconstructed model with an explicit `use_responses_api` value.

For native OpenAI with zero-retention requirements, configure the model kwargs appropriate to the selected API, such as `store=false` and encrypted reasoning content for supported Responses API models. VMA does not infer a compliance policy from the provider name.

### DeepSeek

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=deepseek
DEEPSEEK_API_BASE=
VMA_DEFAULT_DEEPSEEK_MODEL=deepseek-chat
```

The runtime constructs `langchain_deepseek.ChatDeepSeek` through the native integration, rather than pretending DeepSeek is native OpenAI.

Use `deepseek-chat` with VMA's Deep Agents runtime. VMA explicitly marks `deepseek-reasoner` as `tool_calls=false`, and the runtime rejects every non-tool-calling model before graph compilation because the harness itself depends on tool calling. Reasoning models exposed through another provider still need their own tested capability record; a model name alone is not proof of tool support.

### OpenRouter latency profile (deployment default)

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openrouter
VMA_MODEL_PROVIDERS={"openrouter":{"adapter":"openrouter","api_key_env":"OPENROUTER_API_KEY","base_url":"https://openrouter.ai/api/v1","default_model":"deepseek/deepseek-v4-pro","model_kwargs":{"openrouter_provider":{"order":["fireworks","together"],"only":["fireworks","together"],"allow_fallbacks":true,"require_parameters":true,"data_collection":"deny"}},"capabilities":{"streaming":true,"tool_calls":true,"multimodal_input":false,"reasoning":true,"native_structured_output":false}}}
```

The runtime constructs `langchain_openrouter.ChatOpenRouter`. The provider
policy tries Fireworks first and Together second, permits fallback only between
those two providers, rejects endpoints that cannot accept the request
parameters, and requires providers whose data policy denies collection. It does
not use Auto, Exacto, Nitro, or OpenRouter's default provider pool.

This is the platform default, not a global model allowlist. An Agent that
explicitly selects another server-approved provider or model retains that
choice. Tenant runtime fields cannot override the OpenRouter credential slot,
base URL, or provider-routing policy. The key itself must be supplied by a
matching model Credential in a Session Vault.

The default profile declares `multimodal_input=false`. VMA still validates a
Managed Agents image/document `file_id` against the current Session mount, but
it does not send binary bytes to that text-only route. It replaces the inline
block with a trusted sandbox-path marker so the Agent can use filesystem and
execution tools. To obtain direct image/PDF model input, configure and
contract-test a server-approved model profile with `multimodal_input=true`;
VMA then converts the verified Session copy into LangChain standard base64
image/file blocks, which `ChatOpenRouter` serializes into its native request.

## Selecting a provider on an agent

The public model object may include a provider:

```json
{
  "name": "Research agent",
  "model": {
    "provider": "deepseek",
    "id": "deepseek-chat"
  }
}
```

VMA also recognizes a provider-prefixed model ID:

```json
{
  "model": "deepseek:deepseek-chat"
}
```

When no provider is present, `VMA_DEFAULT_MODEL_PROVIDER` is used. Provider names are lowercased and hyphens normalize to underscores.

An agent's private `runtime.model` object may provide a legacy provider fallback and override these inference parameters:

- `temperature`
- `max_tokens`
- `timeout`
- `max_retries`
- `top_p`
- `reasoning_effort`
- `use_responses_api`

Connection fields remain server-owned. Public fields such as Claude's model `speed` may be stored and returned for SDK compatibility without having a portable meaning on another provider.

## Additional providers

`VMA_MODEL_PROVIDERS` is a JSON object keyed by a server-approved provider name. An additional OpenAI-compatible gateway example:

```dotenv
VMA_MODEL_PROVIDERS={"gateway":{"adapter":"openai","api_key_env":"GATEWAY_API_KEY","base_url":"https://models.example/v1","default_model":"vendor-model","model_kwargs":{"use_responses_api":false},"capabilities":{"streaming":true,"tool_calls":true,"multimodal_input":false,"reasoning":false,"native_structured_output":false}}}
```

Readable JSON form:

```json
{
  "gateway": {
    "adapter": "openai",
    "api_key_env": "GATEWAY_API_KEY",
    "base_url": "https://models.example/v1",
    "default_model": "vendor-model",
    "model_kwargs": {
      "use_responses_api": false
    },
    "capabilities": {
      "streaming": true,
      "tool_calls": true,
      "multimodal_input": false,
      "reasoning": false,
      "native_structured_output": false
    }
  }
}
```

Supported registry fields:

| Field | Meaning |
| --- | --- |
| `adapter` | `anthropic`, `openai`, `deepseek`, or another installed LangChain provider identifier |
| `api_key_env` | Internal Vault credential-slot name. The legacy field name does not mean VMA reads process environment. Required for key-based providers; omitted for `fake` and `ollama`. |
| `base_url` | Server-approved endpoint |
| `default_model` | Model ID used when the agent omits one |
| `model_kwargs` | Constructor defaults controlled by the operator |
| `capabilities` | Runtime compatibility claims described below |

Custom entries default to the `openai` adapter. Therefore a custom provider using an OpenAI-compatible endpoint should explicitly keep `use_responses_api=false` unless its endpoint supports Responses.

`VMA_MODEL_PROVIDERS` must not contain an `api_key` value. Key-based providers
receive only the selected Session Vault credential at runtime. The `fake` and
`ollama` adapters require no key and persist credential source `none`.

For a non-built-in adapter, VMA calls LangChain `init_chat_model`. The corresponding provider package must be installed in the service image. Registration alone does not install code or verify the endpoint.

## Capability records

The runtime capability object contains:

```json
{
  "streaming": true,
  "tool_calls": true,
  "multimodal_input": false,
  "reasoning": false,
  "native_structured_output": false
}
```

Built-in defaults:

| Adapter | Streaming | Tool calls | Multimodal input | Reasoning | Native structured output |
| --- | --- | --- | --- | --- | --- |
| `anthropic` | yes | yes | yes | yes | yes |
| `openai` | yes | yes | yes | yes | yes |
| `deepseek` / `deepseek-chat` | yes | yes | no | yes | yes, as configured |
| `deepseek` / `deepseek-reasoner` | yes | **no; rejected by VMA runtime** | no | yes | provider-dependent |
| `openrouter` / DeepSeek V4 Pro profile | yes | yes | no | yes | no, as configured |
| custom `openai` adapter | yes by default | yes by default | yes by adapter default | yes by adapter default | no unless declared |

These are routing assertions, not certifications. Before enabling a model for tenants, contract-test:

- Streaming text and tool-call argument fragmentation.
- Parallel and sequential tool calls.
- The largest tool schema VMA allows.
- Tool error and retry behavior.
- Multimodal content types and size limits.
- Structured output under the actual model ID.
- Context-window overflow and Deep Agents compaction.
- Usage/token reporting.
- Cancellation and timeout behavior.
- Data retention and regional processing.

If a provider cannot satisfy a required feature, fail the run before model invocation or expose a clear session error. Do not silently drop tools or reinterpret a reasoning-only model as tool-capable.

## Provider variance that cannot be normalized

Even OpenAI-compatible endpoints disagree about accepted request fields, system-message handling, JSON schema, tool-choice syntax, parallel calls, reasoning output, image blocks, usage chunks, and error status codes. Deep Agents assumes a LangChain tool-calling model but cannot repair an endpoint that violates those contracts.

Claude-specific managed behavior also remains provider-dependent:

- Prompt caching and cache accounting.
- Compaction quality and token thresholds.
- Hosted web/search tools.
- Anthropic system skills.
- Model `speed` semantics.
- Safety filters and refusal formatting.
- Long-running request limits and retry policy.

See [known incompatibilities](./known-incompatibilities.md#model-and-provider-behavior) and the top-level [compatibility matrix](./compatibility-matrix.md).
