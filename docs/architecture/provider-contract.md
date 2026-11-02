# Provider Contract

> Part of the [Linch architecture guide](./README.md).

Built-in providers subclass `BaseProvider`, which defines two abstract methods
and a concrete `capabilities()` default. Custom providers can satisfy the runtime
interface structurally; the runtime does not require `isinstance(provider, BaseProvider)`:

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from linch import ProviderCapabilities, ProviderRequest


class BaseProvider(ABC):
    id: str

    @abstractmethod
    def context_window(self, model: str) -> int: ...

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]: ...

    def capabilities(self, model: str) -> ProviderCapabilities:
        # Uses ProviderCapabilities defaults; override unsupported features.
        return ProviderCapabilities(context_window=self.context_window(model))
```

`stream()` returns an async iterator; a concrete implementation can use
`async def stream(...)` with `yield` to implement an async generator. It yields
**normalized dicts** — never raw API objects. Required keys by event type:

| `type` value | Required additional keys |
|---|---|
| `"message_start"` | `model: str` |
| `"text_delta"` | `text: str` |
| `"tool_use_start"` | `id: str`, `name: str` |
| `"tool_use_input_delta"` | `id: str`, `json_delta: str` |
| `"tool_use_end"` | `id: str` |
| `"thinking_delta"` | `text: str`, `signature?: str` |
| `"redacted_thinking"` | `data?: object` (converted to a string by the loop) |
| `"message_end"` | `stop_reason: StopReason`, `usage: Usage`, `provider_metadata?: dict[str, object] \| None` |

The loop assembles these — it never imports any provider's raw types. Adding a new provider means implementing this dict contract only.

The event vocabulary is a strict boundary: an adapter must emit the
declared `type` and required fields with the documented normalized shapes.
Malformed events are provider errors; the loop does not silently accept raw
SDK objects, infer missing tool IDs, or turn unknown event shapes into a
successful assistant response. `redacted_thinking` is the one intentionally
opaque content marker; its optional payload is converted to the normalized
redacted-thinking block. Normalize vendor event formats inside the adapter.

A stream must start with exactly one `message_start` and finish with one
`message_end`, with no events after it. Tool IDs and names must be nonempty;
every started tool call must end before `message_end`.

`message_end.usage` must be an actual `linch.Usage` instance, not a mapping or a
provider SDK object, with nonnegative integer token buckets. Stop reasons and
tool calls must agree: `"tool_use"` requires at least one completed tool call,
while every other stop reason forbids completed tool calls in that message.
The accepted stop-reason vocabulary is the public `StopReason` literal.

## Constructing a provider request

Custom adapters receive the public `ProviderRequest` type. Its required fields
are `model`, `system`, `tools`, and `messages`; optional controls have defaults:

```python
from linch import Message, ProviderRequest, SystemBlock, TextBlock

request = ProviderRequest(
    model="my-model",
    system=[SystemBlock(text="Answer concisely.")],
    tools=[],
    messages=[Message(role="user", content=[TextBlock(text="Hello")])],
    max_output_tokens=256,
)

async for event in provider.stream(request):
    print(event)
```

## Optional lifecycle hooks

Beyond the three core methods, providers **may** expose two optional,
duck-typed lifecycle methods. Both are detected with `getattr`/`hasattr`, so a
provider that omits them keeps its current behavior — neither is required on
`BaseProvider`.

- **`async def prepare(self) -> None`** — coalesced and awaited once before the
  first run, for one-time async warm-up that must not run on the construction
  path. llama.cpp probes `/v1/props` or `/props`; vLLM and SGLang probe
  `/v1/models` by default through the same async client that serves chat. Context windows are cached;
  `context_window(model)` is a pure configured/cached lookup with no network I/O.
  Probe failure uses the adapter's fallback.
- **`async def aclose(self) -> None`** — closes an owned HTTP transport.
  Implemented by the Anthropic and OpenAI Responses providers so agent teardown
  releases sockets deterministically. Must be idempotent.

Conformance is exercised network-free by `assert_provider_contract` from
`linch`:

```python
from linch import assert_provider_contract

await assert_provider_contract(lambda: MyProvider(), model="my-model")
```

It checks the provider id, a positive context window, the capability shape
(including `caps.context_window == context_window(model)`), that `prepare()` is
callable when present, and that closing twice is idempotent. A provider must not
advertise a capability its request builder ignores. This helper checks provider
shape and lifecycle only; it does not consume `stream()` or validate its events.
Test event sequences separately against the normalized contract above.

## Chat adapter ownership and local servers

`OpenAIChatCompletionsProvider`, `DeepSeekProvider`, `LlamaCppProvider`,
`VLLMProvider`, and `SGLangProvider` independently implement the public provider
contract. The latter four are not subclasses of the public OpenAI provider.
The private `_ChatCompletionsProvider` in `providers/openai_chat.py` shares only
transport, message serialization, and stream mechanics. Its abstract
`_reasoning_text` and `_usage_from_chunk` hooks require each adapter to normalize
its own response semantics. Each concrete adapter declares capabilities and
context behavior explicitly; local model names never inherit OpenAI catalog
windows or capability assumptions. OpenAI retains its own known-model defaults.

vLLM reads streamed `delta.reasoning` first, then `delta.reasoning_content`;
assistant history continues to serialize `reasoning_content`. Other chat adapters
explicitly read `reasoning_content`. Turning off partial delivery
(`stream_partials=False`) still consumes the same upstream stream; it does not
select a non-streaming transport.

vLLM and SGLang options default to `context_window=None`,
`auto_context_window=True`, and `context_window_timeout=2.0`. One asynchronous
`prepare()` probe uses the shared private `providers/_context_probe.py` helper,
through the chat client so credentials, base path, and redirects match, and never
runs when neither `base_url` nor `OPENAI_BASE_URL` selects a server. It caches
positive integer `max_model_len` values from `/v1/models` by exact model id. Missing, null, nonpositive, or otherwise invalid values are ignored. A server
listing exactly one model applies its value to any alias. Other unknown ids and
failed probes fall back to `128_000`. An explicit `context_window` skips
the probe; `auto_context_window=False` also disables discovery. New options are
appended after the original positional fields.

vLLM and SGLang support model-specific `chat_template_kwargs` and
`disable_thinking_kwargs`. They declare `can_disable_thinking=True` only when
the latter is not `None`; disabled-thinking requests merge these configured kwargs
into template settings. Linch guesses no model key: for example configure
`disable_thinking_kwargs={"enable_thinking": False}` for a Qwen3 template;
Granite 4 also uses `enable_thinking`, while Granite 3.2 uses `thinking`.
See the [vLLM reasoning guide](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)
and [configuration examples](../usage/providers.md#local-server-configuration).
llama.cpp's existing thinking controls are unchanged.

Capabilities describe adapter behavior and do not guarantee model support or
required server launch settings. Tools require both a suitable model/template
and the server configuration:

| Server | Tools | Reasoning | Cache reporting / metrics |
|---|---|---|---|
| vLLM | `--enable-auto-tool-choice --tool-call-parser <parser>` | `--reasoning-parser <parser>` | `--enable-prompt-tokens-details` |
| SGLang | `--tool-call-parser <parser>` | `--reasoning-parser <parser>` | `--enable-cache-report`; `--enable-metrics` for server metrics |
| llama.cpp | `--jinja` with a suitable tool template | Model/template-specific | Request `cache_prompt`; observed reads in `timings.cache_n` |

References: [vLLM CLI](https://docs.vllm.ai/en/stable/cli/serve/),
[SGLang arguments](https://docs.sglang.io/docs/advanced_features/server_arguments),
[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
vLLM can receive optional per-tenant `cache_salt` through existing `extra_body`
pass-through, for example `extra_body={"cache_salt": "tenant-id"}`.

vLLM/SGLang `prompt_cache=True` declares server-automatic prefix caching, with no
request-level cache control. llama.cpp's declaration corresponds to the actual
`cache_prompt` request field. SGLang's `enable_cache_report` options field remains
accepted but is never sent, even if `True`; reporting requires the server flag.
SGLang sampling fields are flat: `sampling_params={"top_k": 50}` becomes
`extra_body={"top_k": 50}`, with explicit `extra_body` values winning conflicts
except for a run's `temperature`, which always wins.

If `usage.prompt_tokens_details` is absent, vLLM/SGLang log a WARNING once per
provider instance naming `--enable-prompt-tokens-details`/`--enable-cache-report`.
Zero with this warning means unknown reuse; present `cached_tokens=0` details
mean a valid miss. vLLM maps details `cached_tokens` to `Usage.cache_read_tokens`
and `created_cache_tokens` to `Usage.cache_creation_tokens`, with
`input_tokens=max(prompt_tokens-cache_read_tokens-cache_creation_tokens, 0)`.
llama.cpp retains `timings.cache_n` cache-read accounting. No `Usage` or persisted
schema changes are involved.

For tuning, use `Usage.cache_read_tokens`, the existing span attribute
`gen_ai.usage.cache_read.input_tokens`, and
`report.summary["prompt_cache"]["cache_read_ratio"]` on `RunReport`. The ratio is
read / (input + read + creation), or zero for an empty denominator. Keep static
tools/system content first and dynamic content later, and inspect
`prompt_cache_advisory` reasons `tool_set_changed` and `model_changed` when reuse
drops. See [prompt caching](../usage/providers.md#prompt-caching).

## Native catalog rendering

`ProviderCapabilities.catalog_changes` is `Literal["append_only", "positional"]`,
defaulting to `"append_only"`, including in `full_capabilities`.
`AnthropicProviderOptions.native_catalog_changes` and
`OpenAIResponsesProviderOptions.native_catalog_changes` both default to `False`.
**Native behavior is UNVERIFIED against live endpoints**; coverage uses fake
transports. Eligibility gates describe the implementation, not a guarantee of
endpoint support. Bedrock, Google Cloud, and unsupported configurations use
append-only rendering. Disabled options use append-only rendering, including
historical catalog blocks carrying definitions. Exact model and endpoint gates
are listed in
[provider usage](../usage/providers.md#native-catalog-changes-opt-in).

When session catalog snapshots are configured, eligibility uses the effective
per-turn model, including skill overrides, and
requires a nonempty snapshot tool prefix without run/context tool narrowing or a
snapshot bypass. A private request decision survives exact model-input replay;
forced-final requests that strip tools also suppress historical native definitions.
A fallback to append-only expands eligible historical frozen definitions into the
tool list, preserving existing rows and the offered-tool boundary.

Catalog reminder text always stays user-role. Only provider-native tool structures
receive system/developer roles:

- Anthropic buffers `tool_addition`/`tool_removal` blocks in a tool-only system
  message immediately before the next assistant message or at the array end,
  never as the first entry or between a tool call and its merged user results.
  A new definition uses `{"type": "tool_definition", "definition": ...}` inside
  `tool_addition.tool`; restoring/removing declared prefix tools uses
  `{"type": "tool_reference", "name": ...}`. Active-name tracking skips duplicate
  additions and inactive removals. A by-value removal stays text-only, and restore
  sends no second definition. Historical by-value provenance keeps removals
  text-only after schema promotion; once compaction drops that definition from
  the provider view, a promoted prefix name can use its declared reference.
  Only an unfinished trailing tool-system message can move after an abort and a
  subsequent user prompt; completed historical placement stays fixed. The last
  user message retains the cache breakpoint. `pause_turn` ends the Linch turn
  and is not resumed.
- Every positional Anthropic request includes `inline-tools-2026-09-15`, merged
  with existing beta headers case-insensitively, even without a tool block.
  Translated inline definition JSON is counted in UTF-8 bytes across the request;
  more than 4,194,304 bytes raises actionable `ConfigError` before dispatch.
- Responses emits `{"type": "additional_tools", "role": "developer", "tools": ...}`
  at each catalog block's position, without `prompt_cache_breakpoint`. Active names
  and duplicate definitions are computed from the whole history before
  `previous_response_id` suffix slicing. Only additions in that suffix are sent;
  names promoted into prefix tools skip stale additional definitions. When tools
  have changed and choice is automatic, `tool_choice` becomes
  `{"type": "allowed_tools", "mode": "auto", "tools": [...]}` in stable name order.
  Forced and `"none"` choices remain unchanged.

Schema edits replace the offered tool definitions; the adapters do not invent native
redefinition semantics. Compaction retains the prefix, resets notification digests,
and synchronizes the consolidated catalog. Audit history retains
native provenance for schema comparisons and durable contract validation even when
the compacted provider view no longer contains the original notice.

## Design rationale

- **Three methods, nothing more.** A provider only has to answer "how big is the
  window", "stream this request", and "what do you support". Keeping the surface tiny is
  what makes a new provider (or a local llama.cpp server) a small, self-contained file.
- **Normalized dicts, never raw API objects.** `stream()` yields a fixed event
  vocabulary, so the loop never imports a vendor SDK's types. Vendor churn stays
  contained in one provider file instead of leaking into the core loop.
- **`capabilities()` + request downgrade instead of per-provider branches in the
  loop.** Providers *declare* what they support; `apply_provider_capabilities` strips
  unsupported fields before each call. The loop has no `if provider == "openai"`
  branches — feature differences are data, handled in one place.
- **Structured-output transport is explicit.** `structured_output` says a
  provider can return a checked JSON result; `structured_output_terminal_tool`
  is true only when that result is represented by a synthetic final tool call.
  Native JSON-schema and JSON-object providers finish through normal response
  parsing, avoiding accidental tool-name collisions.
- **Capability defaults must match the adapter.** The base `capabilities()` derives
  the context window and uses `ProviderCapabilities` defaults:
  `parallel_tool_calls=True`, `structured_output=True`, `tool_choice=True`,
  `structured_output_terminal_tool=False`, `prompt_cache=False`,
  `can_disable_thinking=False`, and `catalog_changes="append_only"`. Override
  unsupported capabilities explicitly, and enable caching or terminal-tool output
  only when the adapter implements them.

---

Back to the [architecture index](./README.md).
