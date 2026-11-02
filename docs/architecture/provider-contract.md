# Provider Contract

> Part of the [Linch architecture guide](./README.md).

Every provider implements `BaseProvider` (three methods):

```python
class BaseProvider(ABC):
    id: str

    def context_window(self, model: str) -> int: ...

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]: ...

    def capabilities(self, model: str) -> ProviderCapabilities:
        # Default derives context_window only; override to declare full support
        return ProviderCapabilities(context_window=self.context_window(model))
```

`stream()` yields **normalized dicts** — never raw API objects. Required keys by event type:

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

The vocabulary is a strict boundary in Linch 2.0: an adapter must emit the
declared `type` and required fields with the documented normalized shapes.
Malformed events are provider errors; the loop does not silently accept raw
SDK objects, infer missing tool IDs, or turn unknown event shapes into a
successful assistant response. `redacted_thinking` is the one intentionally
opaque content marker; its optional payload is converted to the normalized
redacted-thinking block. Keep compatibility shims inside the adapter.

`message_end.usage` must be an actual `linch.Usage` instance, not a mapping or a
provider SDK object. Stop reasons and tool calls must agree: `"tool_use"` requires at
least one completed tool call, while every other stop reason forbids completed tool calls
in that message. The accepted stop-reason vocabulary is the public `StopReason` literal.

## Optional lifecycle hooks

Beyond the three core methods, providers **may** expose two optional,
duck-typed lifecycle methods. Both are detected with `getattr`/`hasattr`, so a
provider that omits them keeps its current behavior — neither is required on
`BaseProvider`.

- **`async def prepare(self) -> None`** — coalesced and awaited once before the
  first run, for one-time async warm-up that must not run on the construction
  path. The llama.cpp provider uses it to probe `/props` off the event loop and
  cache the discovered context window; its `context_window()` stays a pure
  cached/configured lookup and never performs network I/O. Probe failure falls
  back to the configured value.
- **`async def aclose(self) -> None`** — closes an owned HTTP transport.
  Implemented by the Anthropic and OpenAI Responses providers so agent teardown
  releases sockets deterministically. Must be idempotent.

Conformance is exercised network-free by `assert_provider_contract` from
`linch`:

```python
from linch import assert_provider_contract

assert_provider_contract(lambda: MyProvider(), model="my-model")
```

It checks the provider id, a positive context window, the capability shape
(including `caps.context_window == context_window(model)`), that `prepare()` is
callable when present, and that closing twice is idempotent. A provider must not
advertise a capability its request builder ignores.

## Native catalog rendering

`ProviderCapabilities.catalog_changes` is `Literal["append_only", "positional"]`,
defaulting to `"append_only"`, including in `full_capabilities`.
`AnthropicProviderOptions.native_catalog_changes` and
`OpenAIResponsesProviderOptions.native_catalog_changes` both default to `False`.
**Native behavior is UNVERIFIED against live endpoints**; coverage uses fake
transports. Disabled options preserve phase-2 payload bytes, including historical
catalog blocks carrying definitions. Exact model and endpoint gates are listed in
[provider usage](../usage/providers.md#native-catalog-changes-opt-in).

Eligibility uses the effective per-turn model, including skill overrides, and
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

Schema edits take the phase-2 replacement path; the adapters do not invent native
redefinition semantics. Compaction retains the prefix, resets notification digests,
and uses existing synchronization to consolidate changes. Audit history retains
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
- **Capability default is conservative.** The base `capabilities()` derives only the
  context window; a provider must opt in to advertising parallel tool calls / structured
  output / caching. A provider that forgets to override under-promises (safe) rather than
  over-promising (broken calls).

---

Back to the [architecture index](./README.md).
