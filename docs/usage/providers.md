# Providers

Use this page to choose and configure a model provider and inspect its capabilities.

[← Usage guide](./README.md)

Linch shares agent-loop, tool, event, and memory abstractions across providers.
Adapters differ in capabilities, caching, reasoning controls, wire formats, and
endpoint requirements. A **provider** turns Linch's `ProviderRequest` into an
API call and streams normalized events back to the loop. Choose an adapter for
the API you are using.

---

## When to pick which

| You want to talk to… | Use |
|---|---|
| OpenAI reasoning-native models (`o1`, `o3`, `gpt-5`) with effort/summary controls and stateful `previous_response_id` | `OpenAIResponsesProvider` |
| Standard OpenAI Chat Completions (`gpt-4o`, `gpt-5-nano`) | `OpenAIChatCompletionsProvider` |
| Endpoints implementing supported OpenAI Chat shapes (including compatible Azure, Groq, Together deployments) | `OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(base_url=...))` |
| DeepSeek native API — thinking controls, tool-loop reasoning, JSON-object output | `DeepSeekProvider` |
| Anthropic Claude — adaptive thinking, prompt caching, signed thinking blocks | `AnthropicProvider` |
| Google Gemini — large context windows, Google tool semantics | `GeminiProvider` (`[gemini]` extra) |
| A self-hosted `llama.cpp` server | `LlamaCppProvider` |
| A self-hosted vLLM server | `VLLMProvider` |
| A self-hosted SGLang server | `SGLangProvider` |

The two axes that matter most:

- **Conversation chaining.** OpenAI Responses uses `previous_response_id`
  automatically by default when available, sending the new conversation suffix.
  Instructions and tools are still sent. Chaining can be disabled through
  `OpenAIReasoning(previous_response_id="disabled")`. Chat-style adapters send
  conversation history with each request; other caching and state mechanisms
  depend on the adapter and endpoint.
- **Thinking / reasoning round-tripping.** Reasoning models emit intermediate
  "thinking" content (Anthropic thinking blocks, OpenAI reasoning summaries, or
  `reasoning_content` from OpenAI-compatible models like DeepSeek). Linch
  preserves reasoning metadata for subsequent tool turns — see
  [Reading thinking events](#reading-thinking-events).

For the data flow behind the provider interface (`context_window`, `stream`,
`capabilities`), see [Provider contract](../architecture/provider-contract.md).

---

## Configuring a provider

Linch ships several providers. Pick one based on the API you're targeting.

```python
import os
from linch import Agent
from linch import InMemorySessionStore

# ── OpenAI Responses API (o1, o3, gpt-5 — reasoning-native models) ──────────
# Chaining sends a conversation suffix when available; instructions/tools remain.
# Supports native reasoning effort/summary levels and encrypted reasoning tokens.
from linch import OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from linch import OpenAIReasoning

agent = Agent(
    model="gpt-5",
    provider=OpenAIResponsesProvider(
        OpenAIResponsesProviderOptions(
            api_key=os.environ["OPENAI_API_KEY"],
            reasoning=OpenAIReasoning(effort="high"),
        )
    ),
    session_store=InMemorySessionStore(),
)

# ── OpenAI Chat Completions (gpt-4o, gpt-5-nano, compatible endpoints) ─────
# Stateless: full message array resent every turn.
# Requires an endpoint implementing the supported Chat Completions shapes.
# Thinking events emitted when model streams reasoning_content (e.g. DeepSeek).
# Set include_partial_messages=True to receive PartialAssistantEvent for streaming.
from linch import OpenAIChatCompletionsProvider
from linch import OpenAIChatProviderOptions

agent = Agent(
    model="gpt-5-nano-2025-08-07",
    provider=OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=os.environ["OPENAI_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,   # stream text + thinking deltas
)

# ── Anthropic Claude ─────────────────────────────────────────────────────────
# This example configures adaptive thinking and effort. Support for these
# controls and native output_config.format depends on the selected model/API.
# include_partial_messages=True streams ThinkingBlock deltas as kind="thinking" events.
from linch import AnthropicProvider, AnthropicProviderOptions

agent = Agent(
    model="claude-sonnet-4-6",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            thinking={"type": "adaptive"},
            effort="high",
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ``None`` leaves thinking at the model/provider default. To turn it off on a
# model that supports disabling it, send the native mode explicitly:
provider = AnthropicProvider(
    AnthropicProviderOptions(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        thinking={"type": "disabled"},
    )
)

# ── Google Gemini ────────────────────────────────────────────────────────────
# Requires: pip install "linch[gemini]" (uses google-genai)
# Supports text, tool use, structured output, and large context windows.
from linch import GeminiProvider, GeminiProviderOptions

agent = Agent(
    model="gemini-2.5-pro",
    provider=GeminiProvider(
        GeminiProviderOptions(api_key=os.environ["GOOGLE_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
)

# Vertex AI uses the same SDK with application default credentials:
provider = GeminiProvider(
    GeminiProviderOptions(project="my-project", location="us-central1")
)

# Native GenerateContentConfig defaults, including thinking on supported models.
# Per-run thinking={"type": "enabled", "budget_tokens": 1024} overrides this.
# Native thinking_budget/thinking_level dictionaries also work per run.
# Budget ranges and support for disabling thinking depend on the model.
provider = GeminiProvider(
    GeminiProviderOptions(
        api_key=os.environ["GOOGLE_API_KEY"],
        default_generation_config={
            "thinking_config": {"thinking_budget": 1024, "include_thoughts": True},
        },
    )
)
# Thought text streams as thinking_delta; output usage includes thought tokens.
# Linch retains signed model parts in message metadata for subsequent tool turns.
# Keep provider_metadata when copying assistant messages. Tools execute in Linch.
# Gemini uses native JSON Schema for tool parameters and OutputSchema responses.
# Agent teardown closes the provider; standalone callers should await provider.aclose().

# ── DeepSeek native endpoint ────────────────────────────────────────────────
# DeepSeekProvider preserves reasoning_content for subsequent tool turns,
# sends a thinking toggle, and uses JSON-object mode for OutputSchema.
# Model availability and thinking behavior depend on the deployment.
# With thinking enabled, DeepSeek accepts automatic/none tool selection only:
# disable thinking before using a required or named tool_choice.
from linch import DeepSeekProvider, DeepSeekProviderOptions

agent = Agent(
    model="deepseek-v4-flash",
    provider=DeepSeekProvider(
        DeepSeekProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            thinking="enabled",  # use "disabled" for no reasoning
            effort="high",
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── llama.cpp server ─────────────────────────────────────────────────────────
# Uses llama.cpp's OpenAI-compatible /v1/chat/completions route.
# Streaming uses stream=True and includes stream_options by default. Set
# include_stream_options=False for older servers that reject that field.
# The provider uses llama.cpp's response_format schema shape.
# Context window is auto-detected from /v1/props or /props when available.
from linch import LlamaCppProvider, LlamaCppProviderOptions

agent = Agent(
    model=os.environ["LLAMACPP_MODEL"],
    provider=LlamaCppProvider(
        LlamaCppProviderOptions(
            api_key=os.environ["LLAMACPP_API_KEY"],
            base_url=os.environ["LLAMACPP_BASE_URL"],
            chat_template_kwargs={"enable_thinking": False},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── vLLM server ──────────────────────────────────────────────────────────────
# Uses vLLM's OpenAI-compatible /v1/chat/completions route.
# Server-specific request fields can be passed through extra_body.
from linch import VLLMProvider, VLLMProviderOptions

agent = Agent(
    model=os.environ["VLLM_MODEL"],
    provider=VLLMProvider(
        VLLMProviderOptions(
            api_key=os.environ.get("VLLM_API_KEY", "EMPTY"),
            base_url=os.environ["VLLM_BASE_URL"],
            # Context is probed from /v1/models during provider preparation.
            extra_body={"top_k": 40},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── SGLang server ────────────────────────────────────────────────────────────
# Uses SGLang's OpenAI-compatible chat completions endpoint.
# The provider sends OpenAI stream_options by default so SGLang streams token
# usage (input/output, plus cache details when the server reports them). Set
# include_stream_options=False for servers that reject that OpenAI field.
# Launch SGLang with --enable-cache-report to report cache usage.
# sampling_params are flattened into extra_body; explicit extra_body wins.
from linch import SGLangProvider, SGLangProviderOptions

agent = Agent(
    model=os.environ["SGLANG_MODEL"],
    provider=SGLangProvider(
        SGLangProviderOptions(
            api_key=os.environ.get("SGLANG_API_KEY", "EMPTY"),
            base_url=os.environ["SGLANG_BASE_URL"],
            # Context is probed from /v1/models during provider preparation.
            sampling_params={"top_p": 0.9},
            extra_body={"custom": "value"},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── DeepSeek via Anthropic-compatible endpoint (limited compatibility) ───────
agent = Agent(
    model="deepseek-v4-flash",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/anthropic",
            api_mode="compatible",
        )
    ),
    session_store=InMemorySessionStore(),
)
```

A few practical notes on the snippets above:

- **`include_partial_messages=True`** is what turns on streaming
  `PartialAssistantEvent`s (text *and* thinking deltas). Leave it off and you
  still get the full `AssistantEvent` at the end of each turn — set it when you
  want to render tokens as they arrive in a UI.
- **Use `DeepSeekProvider` for the native DeepSeek endpoint.** It preserves
  `reasoning_content` across turns, sends the provider's on/off thinking toggle,
  and uses the JSON-object format DeepSeek supports. The `/anthropic` route is a
  compatibility API: it ignores `budget_tokens` and does not support Claude's
  `output_config.format` JSON schema, so it is unsuitable for strict structured
  support-agent final answers. While native DeepSeek thinking is enabled, use
  automatic tool selection; required or named `tool_choice` is rejected by the
  endpoint, so Linch raises a clear local error instead of sending a 400.
- **`llama.cpp`** resolves its model name and context window from the running
  server (`/v1/props` or `/props`), so you supply whatever model id the server
  reports. Set `chat_template_kwargs={"enable_thinking": False}` to suppress
  thinking output for models that emit it.

---

## Choosing a provider path

Use a direct provider when Linch has native semantics for that API: OpenAI
Responses for stateful reasoning controls, Anthropic for explicit prompt-cache
markers and Claude thinking signatures, Gemini for Google model/tool semantics, and
llama.cpp, vLLM, or SGLang for self-hosted local servers.

Use `OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(base_url=...))` when
a service implements the OpenAI Chat Completions protocol. This is the
path for Azure, Groq, Together, and similar deployments when they implement
the adapter's supported request and response shapes. Use `DeepSeekProvider` for
the native DeepSeek API, where it owns DeepSeek-specific thinking and
JSON-object semantics.

llama.cpp, vLLM, and SGLang model names and context windows are deployment
configuration. `LlamaCppProvider` can auto-detect `n_ctx` from the server.
vLLM and SGLang probe `/v1/models` by default during asynchronous preparation,
using positive integer `max_model_len` values keyed by exact model id; a server
listing exactly one model applies its value to any alias. They fall back to
`128_000` for other unmatched models, invalid/missing values, or probe failure.
These providers are not listed in the static catalog and do not inherit OpenAI
capabilities or known-model context windows, even for matching model names.

### Local-server configuration

For vLLM and SGLang, `context_window=None`, `auto_context_window=True`, and
`context_window_timeout=2.0` are the defaults. `prepare()` performs the probe off
the event loop once; `context_window(model)` only reads configured/cached values
and never makes a network request. An explicit context window skips probing:

```python
# Default discovery (also available on SGLangProviderOptions).
options = VLLMProviderOptions(base_url="http://localhost:8000/v1")
# Override the deployment capacity and skip discovery.
options = VLLMProviderOptions(context_window=32_768)
# Skip discovery and use the 128_000 fallback.
options = VLLMProviderOptions(auto_context_window=False)
```

Capabilities describe adapter support; they do not guarantee the server was
launched with the required parser or that the model/template supports tools or
reasoning. Configure these on the server:

| Server | Tool calling | Reasoning | Cache reporting / metrics |
|---|---|---|---|
| vLLM | `--enable-auto-tool-choice --tool-call-parser <parser>` | `--reasoning-parser <parser>` | `--enable-prompt-tokens-details` |
| SGLang | `--tool-call-parser <parser>` | `--reasoning-parser <parser>` | `--enable-cache-report`; `--enable-metrics` for server metrics |
| llama.cpp | `--jinja` with a suitable tool template | Model/template-specific | Request `cache_prompt`; observed reads in `timings.cache_n` |

See the upstream [vLLM server options](https://docs.vllm.ai/en/stable/cli/serve/),
[SGLang server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments),
and [llama.cpp server guide](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
vLLM tenants can pass `extra_body={"cache_salt": "tenant-id"}` for optional
server-supported prefix-cache isolation; this uses the existing pass-through.

vLLM and SGLang accept `chat_template_kwargs` and `disable_thinking_kwargs`.
`can_disable_thinking` is true only when `disable_thinking_kwargs` is configured
(not `None`). A request with `thinking={"type": "disabled"}` merges those settings
into the configured template kwargs. Choose the key for the actual model/template:

```python
# Qwen3 template example, for either VLLMProviderOptions or SGLangProviderOptions.
options = VLLMProviderOptions(
    chat_template_kwargs={"enable_thinking": True},
    disable_thinking_kwargs={"enable_thinking": False},
)
```

Qwen3 and Granite 4 use `enable_thinking`; Granite 3.2 uses `thinking`. Linch does
not guess a key. Check your deployed template and the
[vLLM reasoning guide](https://docs.vllm.ai/en/stable/features/reasoning_outputs/).
llama.cpp's existing thinking controls are unchanged.

SGLang `sampling_params={"top_k": 50}` produces flat
`extra_body={"top_k": 50}`, not a nested `sampling_params` object. Explicit
`extra_body` fields win on conflicts, except that a run's `temperature` always
wins. See the
[SGLang OpenAI-compatible API](https://docs.sglang.io/docs/basic_usage/openai_api_completions).
`enable_cache_report` remains an options field for compatibility but has no
request-wire effect, even when `True`; use the server launch flag above.

---

<a id="inspecting-the-model-catalog"></a>

## `list_provider_models` and `get_provider_model_info`

Known direct-provider models can be inspected without constructing a live
client — useful for building a model picker UI, validating config at startup, or
reading a model's context window and pricing before you spend a token:

```python
from linch import get_provider_model_info, list_provider_models

for model in list_provider_models("anthropic"):
    print(model.model, model.context_window, model.pricing)

info = get_provider_model_info("gemini-2.5-pro", provider_id="gemini")
print(info.capabilities.structured_output if info else None)
```

`get_provider_model_info` returns `None` when the model is unknown to the static
catalog, so guard the result before reading attributes (as above).

The static catalog covers the built-in direct providers. It intentionally
**excludes** OpenAI-compatible/local models (DeepSeek, llama.cpp, vLLM, SGLang),
because their model lists and context windows come from external configuration
rather than a fixed table.

| Provider id | Path | Static catalog | Pricing |
|---|---|---:|---|
| `openai-responses` | `OpenAIResponsesProvider` | Yes | `None` unless you pass custom pricing |
| `openai-chat` | `OpenAIChatCompletionsProvider` | Yes | `None` unless you pass custom pricing |
| `anthropic` | `AnthropicProvider` | Yes | Known Claude entries from `linch.pricing` |
| `gemini` | `GeminiProvider` | Yes | `None` unless you pass custom pricing |
| `llamacpp` | `LlamaCppProvider` | No, dynamic/self-hosted | `None` unless you pass custom pricing |
| `vllm` | `VLLMProvider` | No, deployment-specific | `None` unless you pass custom pricing |
| `sglang` | `SGLangProvider` | No, deployment-specific | `None` unless you pass custom pricing |
| `deepseek` | `DeepSeekProvider` | No, deployment-specific | `None` unless you pass custom pricing |

Only known Claude models carry pricing out of the box. For every other provider,
cost fields on usage/result events report `None` until you supply a custom
pricing table — see [Events](./events.md#cost-fields) for how to do that.

---

<a id="capability-flags"></a>

## `ProviderCapabilities`

Each catalog record exposes a `ProviderCapabilities` object declaring which
features the provider supports. Before every provider call, Linch downgrades the
`ProviderRequest` to match these capabilities — for example it strips
`output_schema` when the provider declares no structured-output support, and clears
prompt-cache hints for providers that don't cache. You rarely touch this
directly, but it explains why the same `Agent` config behaves correctly across
backends.

| Provider id | Structured output | Tool choice | Prompt cache |
|---|---:|---:|---:|
| `openai-responses` | Yes | Yes | Yes |
| `openai-chat` | Yes | Yes | Yes |
| `deepseek` | Yes (`json_object` + local validation) | Yes | Yes |
| `anthropic` | Yes | Yes | Yes |
| `gemini` | Yes | Yes | Yes |
| `llamacpp` | Yes | Yes | Yes |
| `vllm` | Yes | Yes | Yes |
| `sglang` | Yes | Yes | Yes |

Structured output is supported on every direct provider but reached differently
per backend (native Claude uses `output_config.format`; the explicit Anthropic
compatibility mode uses a generated final-schema tool). The mechanics — and how
schema-repair retries work — live in
[Structured output](./structured-output.md).

---

## Native catalog changes (opt-in)

`AnthropicProviderOptions(native_catalog_changes=True)` and
`OpenAIResponsesProviderOptions(native_catalog_changes=True)` enable positional
catalog changes on supported models. **Both options default to `False` and are
UNVERIFIED against live Anthropic and OpenAI endpoints.** Payload tests use fake
transports. Eligibility gates reflect adapter implementation, not guaranteed
endpoint support. With the option off, providers use append-only rendering.

For example, pass `native_catalog_changes=True` alongside `api_key` in either
provider's options, then use a supported model. The provider reports
`ProviderCapabilities.catalog_changes="positional"` only when eligible; the
capability otherwise defaults to `"append_only"`.

- Anthropic requires the first-party Claude API, effective `api_mode="native"`,
  and an exact model name from this allowlist: `claude-fable-5-1`,
  `claude-mythos-5-1`, `claude-fable-5`, `claude-mythos-5`, `claude-opus-5-5`,
  `claude-opus-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-opus-4-8`.
  There is no prefix matching: `claude-sonnet-5` stays append-only. Explicit
  `base_url` takes precedence over `ANTHROPIC_BASE_URL`; the effective URL must
  have hostname `api.anthropic.com` (or use the default endpoint). Setting
  `api_mode="native"` does not enable proxies. Bedrock and Google Cloud remain
  append-only. Every positional request includes `inline-tools-2026-09-15`.
- Responses requires a GPT model name identifying version 5.4 or later, including
  dated/variant suffixes. Unknown names and `gpt-5.3` stay append-only.

When session catalog snapshots are configured, the session freezes its system
prefix and initial tool definitions in persisted metadata. Native additions
travel in durable conversation blocks while that tool prefix stays fixed.
Notification text remains in user-role messages, including MCP name-only notices; it never gains system/developer authority.
Anthropic sends tool-only system messages containing `tool_addition` definitions
or declared-prefix references and `tool_removal` references. Responses sends
`additional_tools` items at their log positions and maintains an active-name
`allowed_tools` choice for automatic selection. Forced choices and `"none"` are
preserved.

On Anthropic, removing a tool introduced by value is text-only: its wire
availability remains, and invoking it returns an unknown-tool error.
Restoring it does not resend its definition. Retained historical by-value
provenance keeps this rule even after a schema promotion. Duplicate additions
are suppressed after compaction. Anthropic rejects more than 4,194,304 aggregate
UTF-8 bytes of translated inline definitions locally with `ConfigError`; start a
new session or disable the option if this limit is reached.

Input-schema edits, an empty initial tool prefix, run-level `allowed_tools`, and
context tool narrowing use append-only rendering, accepting a cache miss when tool
schemas change. Profile overlays, session tool overrides, effective system-block
overrides, and active request catalogs bypass the snapshot.
Switching to an append-only fallback includes previously added native definitions
in its tool list. A new session adopts the current catalog into its own prefix;
compaction keeps existing prefix bytes and consolidates notices.

For an enabled native run with an existing snapshot, catalog synchronization waits
until context selection is known. Context hooks can therefore run before the new
notice is appended.
See the [provider contract](../architecture/provider-contract.md#native-catalog-rendering)
for wire placement and replay details.

---

## Prompt caching

Caching support and reporting depend on the provider and server. vLLM and
SGLang declare `prompt_cache=True` for server-automatic prefix caching, without
request-level cache control; llama.cpp declares it for its actual `cache_prompt`
request field. The mechanisms differ:

| Provider | Mechanism | What Linch emits |
|---|---|---|
| `anthropic` | Explicit `cache_control` breakpoints | Marks the last static system block **and** the last user message — caches `tools → system → conversation`, the prefix growing each turn |
| `llamacpp` | Explicit `cache_prompt` | Sends `cache_prompt: true` in the request body |
| `sglang` | Server-automatic prefix cache | Reads returned cache details; launch with `--enable-cache-report`; no request-level cache switch |
| `openai-chat` | Automatic prefix cache | Requests usage with `stream_options.include_usage`; reads cached-token details when returned |
| `deepseek` | Server-managed cache | Requests stream usage and normalizes DeepSeek cache counts in its own adapter |
| `openai-responses` | Automatic prefix cache | Reads cached-token details from response usage |
| `vllm` | Server-automatic prefix cache (APC) | Reads `prompt_tokens_details.cached_tokens` and `created_cache_tokens`; launch with `--enable-prompt-tokens-details`; no request-level cache switch |
| `gemini` | Provider-managed implicit cache | Reads cached-token counts when the response supplies them; this adapter sends no explicit cache-control setting |

Regardless of mechanism, a cache **hit** is surfaced uniformly as
`Usage.cache_read_tokens`, which rolls up into `RunReport`'s cache-read ratio —
these reported counts show observed cache reuse.

If `usage.prompt_tokens_details` is missing, vLLM and SGLang log a WARNING once
per provider instance naming `--enable-prompt-tokens-details` and
`--enable-cache-report`, respectively. A reported zero with this warning means
cache reuse is unknown, not a confirmed miss; present details with
`cached_tokens=0` represent a valid miss. Setting SGLang's compatibility option
`enable_cache_report=True` cannot enable server reporting or suppress the warning.
vLLM maps `created_cache_tokens` to `Usage.cache_creation_tokens` and computes
`input_tokens=max(prompt_tokens-cache_read_tokens-cache_creation_tokens, 0)`.
llama.cpp keeps reading observed cache hits from `timings.cache_n`.

### Tuning from observed usage

Compare `Usage.cache_read_tokens` and the existing OpenTelemetry span attribute
`gen_ai.usage.cache_read.input_tokens` across requests with a stable prefix.
The `RunReport` summary exposes
`report.summary["prompt_cache"]["cache_read_ratio"]`, calculated as cache reads
divided by `input_tokens + cache_read_tokens + cache_creation_tokens` (zero for
an empty denominator). Keep static tools and system content first, dynamic content
later. Inspect `prompt_cache_advisory` reasons `tool_set_changed` and
`model_changed` when reuse drops; these are existing observations, not server
cache controls. The `Usage` schema and persisted format are unchanged.

For a live check of the provider's current prompt-cache behavior, run:

```bash
python scripts/benchmark_live_prompt_cache.py
```

This reads DeepSeek-style `.env` keys (`API_KEY`, `model`, `BASE_URL`,
`ANTHROPIC_BASE_URL`) and reports real `Usage.cache_read_tokens` /
`Usage.cache_creation_tokens` across repeated calls with a stable prefix. Use
`--prefix-salt "$(date +%s)"` when you want a fresh prefix instead of reusing a
provider cache warmed by an earlier benchmark run. Provider comparisons are
isolated by default: the script includes the provider name in the stable prefix.
Pass `--shared-prefix` only when you intentionally want to test whether two
endpoints share the same backend prompt cache.

All adapters normalize prompt-cache usage into separate token buckets:
they report uncached prompt tokens in `input_tokens` and cache hits in
`cache_read_tokens` (with cache writes in `cache_creation_tokens`). Use the
per-call `cache_read` columns when in doubt; warm calls near the full prompt
size mean the cache is working.

To measure cache behavior through the real Linch agent loop and tool-result
turns, use:

```bash
python scripts/benchmark_live_prompt_cache.py --mode tool-loop --tool-turns 3
```

To run the cache-case suite — baseline tool loop, stable/volatile injected
context, rotating tool selection, compaction, and model steering — use:

```bash
python scripts/benchmark_live_prompt_cache.py \
  --mode tool-loop \
  --scenario all \
  --tool-turns 3 \
  --prefix-salt "$(date +%s)"
```

For model steering, pass an alternate model explicitly:

```bash
python scripts/benchmark_live_prompt_cache.py \
  --mode tool-loop \
  --scenario model-steering \
  --steering-model deepseek-chat
```

For an offline check of request-shape cacheability, run:

```bash
python scripts/benchmark_prompt_cache.py
```

The offline benchmark uses a mock provider to simulate prefix-cache reads and writes across a
tool loop. It is useful for comparing stable tools, volatile context, and
per-turn tool selection without live API calls. It does **not** prove real
server-side cache behavior; use provider usage (`cache_read_tokens`) or the
live benchmark script (`scripts/benchmark_live_prompt_cache.py`) for that.

### Keeping the cached prefix stable (what produces hits)

Cache reuse depends on the provider's prefix-matching rules and a stable request
prefix. Linch reuses stable system text until prompt or tool inputs change,
resolves `cwd` at construction, and generates the date for each user message.
Dynamic context is appended after static system blocks with `cacheable=False`.
Two host-controlled actions can reduce cache reuse:

- **Compaction** rewrites the message history into a summary, so the message
  prefix changes from that point. Reuse of the unchanged tools and system
  prefix depends on the provider.
- **Per-turn tool selection** — a `ContextBuilder` that returns a different
  `selected_tools` set each turn changes the offered tool schemas and can
  invalidate the cached prefix. Keep the tool set stable across turns if you
  rely on caching.

Two guarantees back this up. First, per-turn context `system_blocks` are always
forced to `cacheable=False` on the wire request — a `ContextBuilder` cannot move
the Anthropic cache breakpoint onto volatile RAG text even by mistake. Second,
equivalent tool selections are order-stable: `selected_tools={"A", "B"}` and
`{"B", "A"}` produce byte-identical tool schemas (selection follows the parent
registry's insertion order, not set iteration), so an unordered set never
silently reshuffles the prefix.

### The `prompt_cache_advisory` event

When the tool set or the model changes between provider calls on the same
session, Linch emits an observational
[`PromptCacheAdvisoryEvent`](./events.md) (`type == "prompt_cache_advisory"`)
with `reason` of `"tool_set_changed"` or `"model_changed"` and a human-readable
`detail`. It is a **warning, not a rewrite** — Linch never mutates
`selected_tools` for you (that is policy, not mechanism); it just tells you the
cached prefix was invalidated so you can decide whether the churn is intended.
The advisory count and reasons also roll up into `RunReport`'s `prompt_cache`
summary block (`advisory_count`, `reasons`, `tool_selection_changes`) alongside
the `cache_read_ratio`.

Checklist for reliable hits:

- **Stable tools** (don't rotate `selected_tools`).
- **Static system prompt**.
- **RAG/ephemeral content after the static prefix**.
- Use `Agent(cache_ttl="5m")` or `Agent(cache_ttl="1h")` for the supported
  Anthropic breakpoint TTL values.

---

## Reading thinking events

vLLM prefers streamed `delta.reasoning` and falls back to `reasoning_content`;
assistant history still uses `reasoning_content`. Other chat adapters explicitly
read `reasoning_content`. Any provider that emits `reasoning_content` (DeepSeek and other OpenAI-compatible
reasoning models) or Anthropic thinking blocks streams its intermediate
reasoning as `partial_assistant` events with `delta["kind"] == "thinking"`. This
requires `include_partial_messages=True` on the `Agent`. Distinguish thinking
from answer text by the `kind` field:

```python
async for event in session.run("What is 17 × 23?"):
    if event.type == "partial_assistant":
        if event.delta.get("kind") == "thinking":
            print("thinking:", event.delta["text"], end="", flush=True)
        elif event.delta.get("kind") == "text":
            print(event.delta["text"], end="", flush=True)
    elif event.type == "result":
        print("\nanswer:", event.final_text)
```

Thinking deltas are for display only — the loop captures and round-trips the
underlying reasoning signatures automatically so you never have to re-send them
yourself. For the full taxonomy of events this stream produces, see
[Events](./events.md).

---

## Related pages

- [Events](./events.md) — the event types `partial_assistant`/`usage`/`result` carry.
- [Structured output](./structured-output.md) — how `output_schema` is applied per provider.
- [Hooks](./hooks.md) — observe provider calls and the live event stream.
- [Provider contract](../architecture/provider-contract.md) — the `BaseProvider` interface and request pipeline.
