# Key Data Types

> Part of the [Linch architecture guide](./README.md).

```mermaid
classDiagram
    class Agent {
        +model: str
        +provider: BaseProvider
        +tools: ToolRegistry
        +loop_guard: LoopGuard | None
        +hooks: list[Any]
        +max_turns: float
        +permission_engine: PermissionEngine
        +deps: Any
        +output_schema: OutputSchema | None
        +session() Session
    }

    class ProviderRequest {
        +model: str
        +system: list~SystemBlock~
        +tools: list~dict~
        +messages: list~Message~
        +output_schema: OutputSchema | None
        +tool_choice: ToolChoice | None
        +cache_prompt: bool | None
        +cache_ttl: str | None
        +max_output_tokens: int | None
    }

    class ProviderCapabilities {
        +context_window: int
        +parallel_tool_calls: bool
        +structured_output: bool
        +tool_choice: bool
        +prompt_cache: bool
    }

    class LoopGuard {
        +max_identical_tool_calls: int
        +max_consecutive_failures: int
        +force_final_answer: bool
    }

    class ToolResult {
        +content: str
        +summary: str | None
        +is_error: bool
        +metadata: dict | None
        +citations: list~Citation~
        +duration_ms: int
        +truncated: bool
    }

    class ContextBuildResult {
        +system_blocks: list~SystemBlock~
        +messages: list~Message~
        +selected_tools: Any
        +budget: ContextBudget
        +metadata: dict
    }

    class MemoryItem {
        +id: str
        +text: str
        +namespace: str | None
        +metadata: dict
    }

    class ResourceAccess {
        +resource: str
        +mode: Literal~read, write~
    }

    Agent --> LoopGuard
    Agent --> ProviderCapabilities : via provider.capabilities()
    Agent --> ProviderRequest : builds per turn
    ProviderRequest --> ProviderCapabilities : downgraded by
    ToolResult --> ResourceAccess : tool declares
```

## Design rationale

- **`Agent` is immutable config; `Session` is mutable state.** Splitting them lets one
  `Agent` mint many concurrent sessions safely, and makes the config the single thing a
  host reasons about when wiring an agent.
- **`ProviderRequest` is rebuilt per turn and downgraded against
  `ProviderCapabilities`.** The request is assembled fresh each turn so per-turn context
  and capability stripping (drop `cache_*` for non-caching providers, drop
  `output_schema` where unsupported) never leak into the next turn or another provider.
- **`ToolResult` separates the model channel from the host channel.** `content` is the
  compact text the model sees; `summary`/`metadata`/`citations`/`duration_ms` are for
  the host (UI, provenance, telemetry). One return type serves both without forcing the
  model to wade through rich metadata it doesn't need.
- **`ResourceAccess` is declarative, not lock-based.** A tool *declares* what it
  reads/writes; the scheduler derives safe parallelism from those declarations, so
  concurrency is a property of data, not hand-placed locks.

---

Back to the [architecture index](./README.md).
