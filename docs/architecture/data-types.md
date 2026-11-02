# Key Data Types

> Part of the [Linch architecture guide](./README.md).

Use this page when you need the normalized request, capability, context-build,
tool-result, or resource-access shapes exchanged by the kernel and providers.

```python
from linch import ProviderRequest, SystemBlock
```

`SystemBlock` carries `text: str`, `cacheable: bool = False`, and the fixed
`type: Literal["text"] = "text"`; `ProviderRequest` requires `model`, `system`,
`tools`, and `messages`. `Agent.session(...)` is asynchronous and must be
awaited. Empty list/dict defaults in the diagram denote fresh dataclass
factories for each instance, not shared mutable values.

```mermaid
classDiagram
    class Agent {
        +model: str
        +provider: BaseProvider
        +tools: ToolRegistry
        +loop_guard: LoopGuard | None
        +hooks: list[Any]
        +max_turns: int | float
        +permission_engine: PermissionEngine
        +deps: Any
        +output_schema: OutputSchema | None
        +session(id: str | None = None, meta: dict[str, object] | None = None) -> Session
    }

    class ProviderRequest {
        +model: str
        +system: list~SystemBlock~
        +tools: list~dict~
        +messages: list~Message~
        +signal: Any = None
        +max_output_tokens: int | None = None
        +temperature: float | None = None
        +stop_sequences: list~str~ | None = None
        +max_retries: int = 5
        +reasoning: dict~str, Any~ | None = None
        +cache_prompt: bool | None = None
        +cache_ttl: Literal~5m, 1h~ | None = None
        +thinking: dict~str, Any~ | None = None
        +effort: Literal~low, medium, high, xhigh, max~ | None = None
        +output_schema: OutputSchema | None = None
        +tool_choice: ToolChoice | None = None
        +stream_partials: bool | None = None
        +_native_catalog_changes: bool | None = None
    }

    class ProviderCapabilities {
        +context_window: int = 128000
        +parallel_tool_calls: bool = true
        +structured_output: bool = true
        +structured_output_terminal_tool: bool = false
        +tool_choice: bool = true
        +prompt_cache: bool = false
        +can_disable_thinking: bool = false
        +catalog_changes: Literal~append_only, positional~ = append_only
    }

    class LoopGuard {
        +max_identical_tool_calls: int = 3
        +max_consecutive_failures: int = 3
        +force_final_answer: bool = false
    }

    class ToolResult {
        +content: str
        +summary: str = ""
        +is_error: bool = false
        +metadata: dict~str, Any~
        +citations: list~Citation~ = []
        +attachments: list~Any~ = []
        +duration_ms: int = 0
        +truncated: bool = false
        +recovery_hint: str = ""
    }

    class ContextBuildResult {
        +system_blocks: list~SystemBlock~ = []
        +messages: list~Message~ = []
        +selected_tools: Any = None
        +budget: ContextBudget = ContextBudget()
        +metadata: dict~str, Any~
    }

    class ContextBudget {
        +max_tokens: int | None = None
        +used_tokens: int = 0
        +remaining_tokens: int | None = None
        +trimmed: bool = false
    }

    class ResourceAccess {
        +resource: str
        +mode: Literal~read, write~ = read
    }

    Agent --> LoopGuard
    Agent --> ProviderCapabilities : via provider.capabilities()
    Agent --> ProviderRequest : builds per turn
    ProviderRequest --> ProviderCapabilities : downgraded by
    ContextBuildResult --> ContextBudget
```

## Design rationale

`CatalogChangeBlock` is an internal message-content variant with reminder text and
tuples of added and removed tool names, plus
`tool_definitions: tuple[dict[str, Any], ...] = ()` for frozen native additions.
Message codecs preserve these definitions through session store reloads. Providers
and token estimators consume its text; eligible positional adapters also render
its native tool structures. It adds no public top-level name or lifecycle event.
`ProviderCapabilities.catalog_changes` defaults to `"append_only"`; opt-in
providers may report `"positional"` for an eligible model/endpoint.

- **`Agent` owns long-lived configuration and lifecycle; `Session` owns mutable
  conversation state.** Splitting those responsibilities lets one `Agent` mint
  many concurrent sessions safely while the host keeps provider, tool, policy,
  and service ownership at the agent boundary.
- **`ProviderRequest` is rebuilt per turn and downgraded against
  `ProviderCapabilities`.** The request is assembled fresh each turn so per-turn context
  and capability stripping (drop `cache_*` for non-caching providers, drop
  `output_schema` where unsupported) never leak into the next turn or another provider.
- **`ToolResult` separates the model channel from the host channel.** Its
  `content` is the compact provider-facing text; `summary`/`metadata`/`citations`/
  `attachments`/`duration_ms`/`truncated` are rich fields for host consumers
  (UI, provenance, telemetry), while `is_error` and `recovery_hint` also guide
  loop result handling.
- **`ResourceAccess` is declarative, not lock-based.** A tool *declares* what it
  reads/writes; the scheduler derives safe parallelism from those declarations, so
  concurrency is a property of data, not hand-placed locks.

---

Back to the [architecture index](./README.md).
