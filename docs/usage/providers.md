# Providers

[← Usage guide](./README.md)

Linch is provider-agnostic: the agent loop, tools, events, and memory are
identical no matter which model backend you target. A **provider** is the thin
adapter that turns Linch's `ProviderRequest` into a specific API call and
streams the response back as typed events. You pick one based on the API you are
talking to.

---

## When to pick which

| You want to talk to… | Use |
|---|---|
| OpenAI reasoning-native models (`o1`, `o3`, `gpt-5`) with effort/summary controls and stateful `previous_response_id` | `OpenAIResponsesProvider` |
| Standard OpenAI Chat Completions (`gpt-4o`, `gpt-5-nano`) | `OpenAIChatCompletionsProvider` |
| Any OpenAI-compatible endpoint (Azure, Groq, Together, DeepSeek) | `OpenAIChatCompletionsProvider(base_url=...)` |
| Anthropic Claude — extended thinking, prompt caching, thinking signatures | `AnthropicProvider` |
| Google Gemini — large context windows, Google tool semantics | `GeminiProvider` (`[gemini]` extra) |
| A self-hosted `llama.cpp` server | `LlamaCppProvider` |
| A self-hosted vLLM server | `VLLMProvider` |
| A self-hosted SGLang server | `SGLangProvider` |

The two axes that matter most:

- **Stateful vs stateless.** The OpenAI Responses API is *stateful*: Linch sends
  `previous_response_id` so only new messages travel the wire each turn, and the
  server retains reasoning state. Every other path is *stateless*: the full
  message array is resent on every turn.
- **Thinking / reasoning round-tripping.** Reasoning models emit intermediate
  "thinking" content (Anthropic thinking blocks, OpenAI reasoning summaries, or
  `reasoning_content` from OpenAI-compatible models like DeepSeek). Linch
  preserves and re-sends these signals automatically so multi-turn tool loops do
  not break — see [Reading thinking events](#reading-thinking-events).

For the data flow behind the provider interface (`context_window`, `stream`,
`capabilities`), see [`../architecture.md`](../architecture.md).

---

## Configuring a provider

Linch ships several providers. Pick one based on the API you're targeting.

```python
import os
from linch import Agent
from linch.sessions import InMemorySessionStore

# ── OpenAI Responses API (o1, o3, gpt-5 — reasoning-native models) ──────────
# Stateful: sends previous_response_id so only new messages travel the wire.
# Supports native reasoning effort/summary levels and encrypted reasoning tokens.
from linch.providers.openai_responses import OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from linch.openai_responses import OpenAIReasoning

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

# ── OpenAI Chat Completions (gpt-4o, gpt-5-nano, any OpenAI-compatible) ─────
# Stateless: full message array resent every turn.
# Works with any OpenAI-compatible endpoint (Azure, Groq, Together, …).
# Thinking events emitted when model streams reasoning_content (e.g. DeepSeek).
# Set include_partial_messages=True to receive PartialAssistantEvent for streaming.
from linch.providers import OpenAIChatCompletionsProvider
from linch.providers.openai_chat import OpenAIChatProviderOptions

agent = Agent(
    model="gpt-5-nano-2025-08-07",
    provider=OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=os.environ["OPENAI_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,   # stream text + thinking deltas
)

# ── Anthropic Claude ─────────────────────────────────────────────────────────
# Supports extended thinking (budget_tokens), prompt caching, tool use, and
# structured output through a generated final schema tool.
# include_partial_messages=True streams ThinkingBlock deltas as kind="thinking" events.
from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

agent = Agent(
    model="claude-sonnet-4-6",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            thinking={"type": "enabled", "budget_tokens": 5000},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── Google Gemini ────────────────────────────────────────────────────────────
# Requires: pip install "linch[gemini]"
# Supports text, tool use, structured output, and large context windows.
from linch.providers import GeminiProvider, GeminiProviderOptions

agent = Agent(
    model="gemini-2.5-pro",
    provider=GeminiProvider(
        GeminiProviderOptions(api_key=os.environ["GOOGLE_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
)

# ── DeepSeek (OpenAI-compatible endpoint) ────────────────────────────────────
# deepseek-v4-flash / deepseek-v4-pro are reasoning models that emit
# reasoning_content — Linch round-trips it automatically so multi-turn tool
# loops work without 400 errors.
agent = Agent(
    model="deepseek-v4-flash",
    provider=OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── llama.cpp server ─────────────────────────────────────────────────────────
# Uses llama.cpp's OpenAI-compatible /v1/chat/completions route.
# Streaming remains enabled via stream=True; the provider avoids OpenAI's
# stream_options field and uses llama.cpp's response_format schema shape.
# Context window is auto-detected from /v1/props or /props when available.
from linch.providers import LlamaCppProvider, LlamaCppProviderOptions

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
from linch.providers import VLLMProvider, VLLMProviderOptions

agent = Agent(
    model=os.environ["VLLM_MODEL"],
    provider=VLLMProvider(
        VLLMProviderOptions(
            api_key=os.environ.get("VLLM_API_KEY", "EMPTY"),
            base_url=os.environ["VLLM_BASE_URL"],
            context_window=128_000,
            extra_body={"top_k": 40},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── SGLang server ────────────────────────────────────────────────────────────
# Uses SGLang's OpenAI-compatible chat completions endpoint.
# The provider omits OpenAI stream_options by default. Set
# include_stream_options=True if your deployment accepts that OpenAI field.
# SGLang sampling/cache-report controls are exposed through extra_body.
from linch.providers import SGLangProvider, SGLangProviderOptions

agent = Agent(
    model=os.environ["SGLANG_MODEL"],
    provider=SGLangProvider(
        SGLangProviderOptions(
            api_key=os.environ.get("SGLANG_API_KEY", "EMPTY"),
            base_url=os.environ["SGLANG_BASE_URL"],
            context_window=128_000,
            include_stream_options=False,
            sampling_params={"top_p": 0.9},
            enable_cache_report=True,
            extra_body={"custom": "value"},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── DeepSeek via Anthropic-compatible endpoint ───────────────────────────────
agent = Agent(
    model="deepseek-v4-flash",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/anthropic",
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
- **DeepSeek is not a separate provider.** It is reached either through the
  OpenAI Chat Completions path (`base_url="https://api.deepseek.com"`) or, if you
  prefer Anthropic semantics, through `AnthropicProvider` with the
  `/anthropic` base URL. The reasoning model's `reasoning_content` is preserved
  across turns automatically, which is why tool loops don't 400.
- **`llama.cpp`** resolves its model name and context window from the running
  server (`/v1/props` or `/props`), so you supply whatever model id the server
  reports. Set `chat_template_kwargs={"enable_thinking": False}` to suppress
  thinking output for models that emit it.

---

## Choosing a provider path

Use a direct provider when Linch has native semantics for that API: OpenAI
Responses for stateful reasoning controls, Anthropic for prompt caching and
Claude thinking signatures, Gemini for Google model/tool semantics, and
llama.cpp, vLLM, or SGLang for self-hosted local servers.

Use `OpenAIChatCompletionsProvider(base_url=...)` when a service implements the
OpenAI Chat Completions protocol. This is the recommended path for DeepSeek,
Azure, Groq, Together, and similar OpenAI-compatible endpoints. DeepSeek is not
a separate runtime provider in Linch; configure it with `base_url` and the
DeepSeek model id.

llama.cpp, vLLM, and SGLang model names and context windows are deployment
configuration. `LlamaCppProvider` can auto-detect `n_ctx` from the server when
available; vLLM and SGLang default to `128_000` and let you set
`context_window` explicitly. These providers are not listed in the static
catalog.

---

## Inspecting the model catalog

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
| DeepSeek | OpenAI-compatible `base_url` | No separate provider | `None` unless you pass custom pricing |

Only known Claude models carry pricing out of the box. For every other provider,
cost fields on usage/result events report `None` until you supply a custom
pricing table — see [Events](./events.md#cost-fields) for how to do that.

---

## Capability flags

Each catalog record exposes a `ProviderCapabilities` object declaring which
features the provider supports. Before every provider call, Linch downgrades the
`ProviderRequest` to match these capabilities — for example it strips
`output_schema` when the provider has no native structured output, and clears
prompt-cache hints for providers that don't cache. You rarely touch this
directly, but it explains why the same `Agent` config behaves correctly across
backends.

| Provider id | Structured output | Tool choice | Prompt cache |
|---|---:|---:|---:|
| `openai-responses` | Yes | Yes | Yes |
| `openai-chat` | Yes | Yes | Yes |
| `anthropic` | Yes | Yes | Yes |
| `gemini` | Yes | Yes | Yes |
| `llamacpp` | Yes | Yes | Yes |
| `vllm` | Yes | Yes | Yes |
| `sglang` | Yes | Yes | Yes |

Structured output is supported on every direct provider but reached differently
per backend (Anthropic, for instance, uses a generated final-schema tool). The
mechanics — and how schema-repair retries work — live in
[Structured output](./structured-output.md). Prompt caching is Anthropic-only;
enable it via the provider options, not a global flag.

---

## Reading thinking events

Any provider that emits `reasoning_content` (DeepSeek and other OpenAI-compatible
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
- [`../architecture.md`](../architecture.md) — the `BaseProvider` interface and request pipeline.
