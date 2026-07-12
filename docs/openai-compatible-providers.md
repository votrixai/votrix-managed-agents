# Model Providers

VMA uses Deep Agents 0.6.12 with LangChain chat-model integrations. Anthropic, OpenAI, and DeepSeek are first-class adapters; operators can register additional server-approved providers.

The filename is retained for existing links, but provider support is not limited to OpenAI-compatible APIs.

Current status is **implemented for provider resolution and model construction** and **partial for behavioral portability**. A provider capability record cannot make different APIs produce identical tool calls, streaming chunks, reasoning fields, structured output, token accounting, or failure behavior.

## Security model

Agent JSON selects a provider and model ID. It cannot supply an arbitrary base URL or raw API key. Credentials and connection endpoints come from:

1. Session vault credentials of type `environment_variable`, matched by the provider's configured `api_key_env`.
2. Server settings or process environment.
3. A server-owned entry in `VMA_MODEL_PROVIDERS`.

This prevents a tenant from turning model configuration into unrestricted server-side request forgery. Operators must still review every registered base URL, TLS policy, data-retention policy, and credential scope.

VMA constructs model objects explicitly and does not mutate process environment or register tenant-specific Deep Agents provider profiles. Deep Agents provider and harness registries are process-global and are unsafe for per-request secrets.

## Built-in providers

### Anthropic

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=
VMA_DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-6
```

The runtime constructs `langchain_anthropic.ChatAnthropic`. An empty `ANTHROPIC_BASE_URL` uses the provider default.

### OpenAI

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openai
OPENAI_API_KEY=...
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
DEEPSEEK_API_KEY=...
DEEPSEEK_API_BASE=
VMA_DEFAULT_DEEPSEEK_MODEL=deepseek-chat
```

The runtime constructs `langchain_deepseek.ChatDeepSeek` through the native integration, rather than pretending DeepSeek is native OpenAI.

Use `deepseek-chat` with VMA's Deep Agents runtime. VMA explicitly marks `deepseek-reasoner` as `tool_calls=false`, and the runtime rejects every non-tool-calling model before graph compilation because the harness itself depends on tool calling. Reasoning models exposed through another provider still need their own tested capability record; a model name alone is not proof of tool support.

### OpenRouter latency profile (deployment default)

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openrouter
OPENROUTER_API_KEY=...
VMA_MODEL_PROVIDERS={"openrouter":{"adapter":"openrouter","api_key_env":"OPENROUTER_API_KEY","base_url":"https://openrouter.ai/api/v1","default_model":"deepseek/deepseek-v4-pro","model_kwargs":{"openrouter_provider":{"order":["fireworks","together"],"only":["fireworks","together"],"allow_fallbacks":true,"require_parameters":true,"data_collection":"deny"}},"capabilities":{"streaming":true,"tool_calls":true,"multimodal_input":false,"reasoning":true,"native_structured_output":false}}}
```

The runtime constructs `langchain_openrouter.ChatOpenRouter`. The provider
policy tries Fireworks first and Together second, permits fallback only between
those two providers, rejects endpoints that cannot accept the request
parameters, and requires providers whose data policy denies collection. It does
not use Auto, Exacto, Nitro, or OpenRouter's default provider pool.

This is the platform default, not a global model allowlist. An Agent that
explicitly selects another server-approved provider or model retains that
choice. Tenant runtime fields cannot override the OpenRouter API key, base URL,
or provider-routing policy.

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

An agent's private `runtime.model` object may override the provider and these inference parameters:

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
| `api_key_env` | Name resolved from mounted vault secrets or process environment |
| `api_key` | Server-owned direct key; environment or secret-manager indirection is preferred |
| `base_url` | Server-approved endpoint |
| `default_model` | Model ID used when the agent omits one |
| `model_kwargs` | Constructor defaults controlled by the operator |
| `capabilities` | Runtime compatibility claims described below |

Custom entries default to the `openai` adapter. Therefore a custom provider using an OpenAI-compatible endpoint should explicitly keep `use_responses_api=false` unless its endpoint supports Responses.

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
