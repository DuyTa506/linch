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
| `"message_end"` | `stop_reason: StopReason`, `usage: Usage`, `provider_metadata: Any` |

The loop assembles these — it never imports any provider's raw types. Adding a new provider means implementing this dict contract only.

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
- **Capability default is conservative.** The base `capabilities()` derives only the
  context window; a provider must opt in to advertising parallel tool calls / structured
  output / caching. A provider that forgets to override under-promises (safe) rather than
  over-promising (broken calls).

---

Back to the [architecture index](./README.md).
