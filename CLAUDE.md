# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development (all extras)
pip install -e '.[dev,mcp,anthropic]'

# Run all tests
pytest

# Run a single test file
pytest tests/test_agent_loop.py

# Run a single test by name
pytest tests/test_agent_loop.py::test_function_name

# Lint
ruff check .

# Format check
ruff format --check .

# Auto-fix lint/format
ruff check --fix . && ruff format .

# Type check
pyright
```

## Architecture

AgentKit is a Python SDK for embedding a software engineering agent loop in applications. It is async-first, event-driven, and provider-agnostic.

### Core flow

```
Agent (config) → Session (state) → run_loop() → Events → caller
```

1. **Agent** (`agent.py`) — holds immutable config: model, provider, tools, permissions, session_store, system prompt, compaction strategy.
2. **Session** (`session.py`) — per-conversation state: `provider_view` (trimmed for LLM context), `full_history` (complete record). Call `session.run(prompt)` to get an `AsyncIterator[Event]`.
3. **run_loop / stream_turn** (`loop.py`) — the main agent loop. Each turn: build user message → run `ContextBuilder` → call `provider.stream()` → collect text/tool-use blocks → check permissions → execute tools (via `scheduler.py`) → emit events → repeat if more tool calls; stop on text-only response.
4. **Events** (`events.py`) — all communication from the loop is through events: `UserEvent`, `ContextBuildEvent`, `AssistantEvent`, `ToolCallStartEvent`, `ToolCallEndEvent`, `PermissionRequestEvent`, `UsageEvent`, `ResultEvent`, `ErrorEvent`, `CompactionEvent`, `LoopGuardEvent`, and skill/subagent events.

### Providers (`providers/`)

Abstract interface (`BaseProvider`) with three methods: `context_window(model)`, `stream(req) → AsyncIterator[StreamEvent]`, and `capabilities(model) → ProviderCapabilities`. Implementations:
- `OpenAIChatCompletionsProvider` — standard OpenAI Chat API
- `OpenAIResponsesProvider` — OpenAI o1/o3 Responses API (with reasoning tokens)
- `AnthropicProvider` — Anthropic Claude; full streaming with tool use, thinking blocks, and prompt caching (`prompt_cache=True`, `structured_output=False`)
- `with_retry` — wraps any provider callable with exponential-backoff retry

`ProviderCapabilities` declares per-provider feature support (`parallel_tool_calls`, `structured_output`, `tool_choice`, `prompt_cache`, `context_window`). `apply_provider_capabilities(req, caps)` in `loop.py` downgrades a `ProviderRequest` before each call — clears `cache_prompt`/`cache_ttl` for non-caching providers, strips `output_schema` when native structured output is unsupported. Duck-typed test providers that don't implement `capabilities()` are safely skipped via `hasattr` guard.

### Loop Guard (`loop_guard/`)

`LoopGuard` detects obvious agentic loops without extra LLM calls:
- **Repeated identical tool calls** — same name+input called ≥ `max_identical_tool_calls` times (default 3).
- **Consecutive failure streaks** — every tool in a batch fails for ≥ `max_consecutive_failures` turns (default 3).

The guard is **on by default** at `Agent()` construction. Disable with `Agent(loop_guard=None)`. When tripped it emits a `LoopGuardEvent` and either stops immediately (default) or runs one final tools-disabled turn (`force_final_answer=True`). Max-turns exhaustion also emits a `LoopGuardEvent(reason="max_turns")` for clarity alongside the existing `ErrorEvent(TurnLimitError)`.

### Tools (`tools/`)

Tools are **protocols** (duck-typed), not subclasses. Each tool has: `name`, `description`, `input_schema`, `scope`, `parallel_safe`, and methods `validate()`, `execute()`, `summarize()`. V2 tools may also expose `parallel`, `resources(input)`, `tags`, `capabilities`, and `cost_hint`. Built-ins: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`. The `ToolRegistry` holds available tools; `default_tools()` returns the standard set.

`ToolContext` is passed to every `execute()` call and provides: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`, `filesystem` (virtual `FileBackend` when the filesystem subsystem is enabled).

Read tools with `parallel=True` or legacy `parallel_safe=True` may run concurrently. Write and exec tools serialize by default. Optional `ResourceAccess` declarations prevent overlapping read/write conflicts on the same resource. Concurrency limit: `Agent(max_tool_concurrency=...)` or `AGENTKIT_MAX_TOOL_CONCURRENCY` env var (default: CPU count).

**Timeouts**: `Agent(tool_timeout_ms=N)` sets an agent-wide execution deadline (env `AGENTKIT_TOOL_TIMEOUT_MS`). A tool may override with a class-level `execution_timeout_ms` attribute; `0` opts out. Default `None` = no timeout (zero-overhead, backward compatible). A timed-out tool returns `is_error=True` with an actionable message; the run continues. Implemented via `asyncio.wait_for` catching `asyncio.TimeoutError` (Python 3.10 safe). `ToolTimeoutError` (`kind="tool_timeout"`, `retryable=True`) is the typed exception for observer/policy use.

**Retry**: `Agent(tool_retry=RetryOptions(max_attempts=..., base_delay_ms=...))` enables opt-in exponential-backoff retry. Gated by scope: read-scope tools retry any exception (idempotent); write/exec tools only retry when the tool sets `retryable = True`. `AbortError` is never retried.

`ToolResult` supports plain `content` plus `summary`, `metadata`, `citations`, `attachments`, `duration_ms`, and `truncated`. The model receives the compact `content`; host apps can use the richer fields for RAG provenance and UI rendering.

### Context (`context/`)

Use `ContextBuilder.build(turn) -> ContextBuildResult` for RAG, memory recall, ephemeral system blocks, budget trimming, and request-scoped tool selection. Builder output is appended only to the provider request and does not mutate `session.provider_view`. The legacy `context_hooks.py` API has been removed.

### Memory (`memory/`)

`MemoryStore` is a protocol for app-owned memory backends. Core includes `MemoryItem`, `MemorySearchResult`, `InMemoryKeywordMemoryStore`, `SqliteMemoryStore`, `MemoryContextBuilder`, `MemorySearchTool`, and `MemoryUpsertTool`. Do not add vector DB or embedding dependencies to core; adapters should implement the protocol or live in examples/recipes.

### Virtual Filesystem (`filesystem/`)

`FileBackend` is a duck-typed protocol for a virtual, session-scoped filesystem that is separate from the real `cwd` on disk. Four implementations ship: `StateFileBackend` (in-memory, per-session default), `DiskFileBackend` (real files sandboxed under a root, default `.agent_kit/offload`), `SqliteFileBackend` (persistent across sessions), and `CompositeFileBackend` (routes paths by prefix, e.g. `/memories/` → `SqliteFileBackend`).

**Auto-offload**: `Agent(result_offload=OffloadConfig())` enables automatic offloading of tool results that exceed `threshold_tokens` (default 20,000). The scheduler calls `maybe_offload()` at the single result chokepoint in `_execute_one` (`scheduler.py`). The full payload is written to the backend; `ToolResult.content` is replaced with a preview + path hint before the `ToolResultBlock` enters `provider_view`. The full `ToolResult` still travels on `ToolCallEndEvent.tool_result` for observers. `maybe_offload` never raises — a backend write failure silently returns the original result.

**Four tools** register automatically when a backend is configured (`Agent(filesystem=...)` or `Agent(result_offload=...)`): `ls`, `read_file` (supports `offset`/`limit` line windowing), `write_file`, `edit_file`. These are in `OffloadConfig.skip_tools` by default so reading back a large file cannot trigger a recursive re-offload. A filesystem-aware system-prompt block is also added automatically.

`FeatureFlags(filesystem=False)` disables the subsystem even when a backend is passed.

### Permissions (`permissions/`)

`PermissionEngine` evaluates each tool call against configured rules before execution. Modes: `"default"` (prompt user), `"acceptEdits"` (auto-allow file edits), `"skip-dangerous"` (allow all). Rules: `ToolRule`, `BashRule`, `PathRule`. When a tool call is not auto-approved, a `PermissionRequestEvent` is emitted and the loop pauses until the caller responds.

### Sessions & Storage (`sessions/`)

`SessionStore` is a protocol. Implementations: `InMemorySessionStore` (ephemeral) and `SqliteSessionStore` (persistent). The store also handles task management (`Task`, `TaskPatch`, status tracking) used by the `TaskCreate/List/Get/Update` tools.

### MCP Integration (`mcp/`)

`connect_mcp_servers()` connects to external MCP servers (stdio or HTTP) and returns an `McpConnection` that exposes MCP tools as AgentKit tools. MCP tool names are normalized via `mcp/naming.py`.

### Skills & Subagents (`skills/`, `subagents/`)

**Skills** are slash-commands defined as `SKILL.md` files (YAML frontmatter + markdown body) loaded from `.agent_kit/skills/*/SKILL.md`. The skill system supports argument substitution and system-reminder injection.

**Subagents** are specialized agent roles defined in `.agent_kit/agents.yaml`. The subagent registry resolves agent definitions; `runner.py` executes them with their own tool overlays and prompts.

### Compaction (`compaction.py`)

When the provider's context window approaches its limit, the compaction strategy summarizes old messages to free space. This is transparent to the caller; a `CompactionEvent` is emitted.

### Observability (`observability/`)

`RunObserver` is a vendor-neutral protocol with nine span-hook methods (`on_run_start/end`, `on_turn_start/end`, `on_provider_call_start/end`, `on_tool_start/end`) plus an `on_event` catch-all. Methods may be sync or async.

`ObserverDispatcher` fans out hook calls to a list of observers, awaits async results, and swallows exceptions — a faulty observer never crashes a run. Zero-overhead when no observers are attached.

Stdlib reference observers: `LoggingObserver` (one log line per span) and `SpanCollector` (in-memory span list for tests). `OpenTelemetryObserver` is the production integration point, behind the optional `[otel]` extra (lazily imported, `pip install 'agent-kit[otel]'`). Langfuse, LangSmith, Honeycomb, and Datadog are all reached via the OTel adapter — no vendor-specific code in core.

Observers are attached via `Agent(observers=[...])` and accessed as `agent.observers`.

## Key design constraints

- All async — no blocking I/O anywhere in the core loop.
- `provider_view` vs `full_history` are kept separately; only `provider_view` is sent to the LLM. Compaction modifies `provider_view` only.
- Tool protocol is duck-typed — avoid inheriting from a base class when adding tools; implement the protocol attributes directly.
- The loop continues as long as the response contains tool calls; it stops when the model returns a text-only response (or hits a stop condition).
- No vendor observability stack in core — observability backends (Langfuse, LangSmith, etc.) are reached via the OpenTelemetry seam, never as direct dependencies.
- Tool timeouts default to `None` (off) and use `asyncio.wait_for` + `asyncio.TimeoutError` (not the 3.11+ unified builtin) to stay Python 3.10 compatible. Timeouts convert to `is_error=True` results; they never raise out of `_execute_one` so parallel-lane siblings are unaffected.
- Tool retry is side-effect gated: read-scope tools only, or tools that explicitly set `retryable = True`. Write/exec tools are never retried by default.
- Virtual filesystem is opt-in (`Agent(filesystem=...)` or `Agent(result_offload=...)`); zero overhead when unset. `DiskFileBackend` defaults to `.agent_kit/offload` (gitignored). `maybe_offload` is a no-op on error results and filesystem-tool results. A backend write failure silently returns the original result — storage errors never crash a run.
