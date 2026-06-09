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

Linch is a Python SDK for embedding a software engineering agent loop in applications. It is async-first, event-driven, and provider-agnostic.

### Core flow

```
Agent (config) → Session (state) → run_loop() → Events → caller
```

1. **Agent** (`agent.py`) — holds immutable config: model, provider, tools, permissions, session_store, system prompt, compaction strategy.
2. **Session** (`session.py`) — per-conversation state: `provider_view` (trimmed for LLM context), `full_history` (complete record), `workers` (live `WorkerHandle` objects keyed by worker_id), `pending_notifications` (background worker `<task-notification>` Messages drained at top of each turn). Call `session.run(prompt)` to get an `AsyncIterator[Event]`.
3. **run_loop / stream_turn** (`loop.py`) — the main agent loop. Each turn: build user message → run `ContextBuilder` → call `provider.stream()` → collect text/tool-use blocks → check permissions → execute tools (via `scheduler.py`) → emit events → repeat if more tool calls; stop on text-only response.
4. **Events** (`events.py`) — all communication from the loop is through events: `UserEvent`, `ContextBuildEvent`, `AssistantEvent`, `ToolCallStartEvent`, `ToolCallEndEvent`, `PermissionRequestEvent`, `UsageEvent`, `ResultEvent`, `ErrorEvent`, `CompactionEvent`, `LoopGuardEvent`, skill/subagent events, and `BackgroundWorkerEvent` (`worker_id`, `status`, `display_name`; emitted when a background worker completes).

### Providers (`providers/`)

Abstract interface (`BaseProvider`) with three methods: `context_window(model)`, `stream(req) → AsyncIterator[StreamEvent]`, and `capabilities(model) → ProviderCapabilities`. Implementations:
- `OpenAIChatCompletionsProvider` — standard OpenAI Chat API; also drives OpenAI-compatible endpoints (e.g. DeepSeek) via `base_url`
- `OpenAIResponsesProvider` — OpenAI o1/o3 Responses API (with reasoning tokens)
- `AnthropicProvider` — Anthropic Claude; full streaming with tool use, thinking blocks, and prompt caching (`prompt_cache=True`, `structured_output=False`)
- `GeminiProvider` — Google Gemini (optional `[gemini]` extra); cross-chunk tool-call dedup
- `LlamaCppProvider` — local llama.cpp server; inherits the Chat-Completions streaming path
- `with_retry` — wraps any provider callable with exponential-backoff retry

`providers/catalog.py` exposes static model metadata for the built-in direct providers: `list_provider_models(provider_id=None)`, `get_provider_model_info(model, provider_id=None)`, and the `ProviderModelInfo` record (`context_window`, `capabilities`, `pricing`). It intentionally excludes OpenAI-compatible/local models whose model lists depend on external config.

`ProviderCapabilities` declares per-provider feature support (`parallel_tool_calls`, `structured_output`, `tool_choice`, `prompt_cache`, `context_window`). `apply_provider_capabilities(req, caps)` in `loop.py` downgrades a `ProviderRequest` before each call — clears `cache_prompt`/`cache_ttl` for non-caching providers, strips `output_schema` when native structured output is unsupported. Duck-typed test providers that don't implement `capabilities()` are safely skipped via `hasattr` guard.

### Loop Guard (`loop_guard/`)

`LoopGuard` detects obvious agentic loops without extra LLM calls:
- **Repeated identical tool calls** — same name+input called ≥ `max_identical_tool_calls` times (default 3).
- **Consecutive failure streaks** — every tool in a batch fails for ≥ `max_consecutive_failures` turns (default 3).

The guard is **on by default** at `Agent()` construction. Disable with `Agent(loop_guard=None)`. When tripped it emits a `LoopGuardEvent` and either stops immediately (default) or runs one final tools-disabled turn (`force_final_answer=True`). Max-turns exhaustion also emits a `LoopGuardEvent(reason="max_turns")` for clarity alongside the existing `ErrorEvent(TurnLimitError)`.

### Tools (`tools/`)

Tools are **protocols** (duck-typed), not subclasses. Each tool has: `name`, `description`, `input_schema`, `scope`, `parallel_safe`, and methods `validate()`, `execute()`, `summarize()`. V2 tools may also expose `parallel`, `resources(input)`, `tags`, `capabilities`, and `cost_hint`. Built-ins: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`. The `ToolRegistry` holds available tools; `default_tools()` returns the standard set.

**Function tools (`tools/function.py`)**: the `@tool` decorator wraps a plain (sync or async) Python function into a protocol-compatible `FunctionTool` — it infers a minimal JSON schema from the signature/annotations, injects `ToolContext` when the function declares a `ctx` parameter, and accepts `scope`, `parallel`, `tags`, `summary`, `resources`, `retryable`, and `execution_timeout_ms` overrides. No base-class inheritance; the result drops straight into a `ToolRegistry`.

**Execution backends (`tools/execution.py`)**: `ExecutionBackend` is a duck-typed protocol (`run(command, *, cwd, timeout_s, signal) → ExecResult`). `LocalBackend` (default) runs a subprocess shell with process-group kill and abort-aware communicate; `DockerBackend` runs inside `docker run --rm` (guarded by `shutil.which`, no Docker SDK), with configurable network/mount/read-only/user. Inject via `Agent(execution_backend=...)`, which replaces only the `Bash` tool (never adds one to a registry that excludes it). Both backends route timeout/abort through `_communicate_with_timeout_and_abort` so `session.abort()` interrupts in-flight commands.

`ToolContext` is passed to every `execute()` call and provides: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`, `filesystem` (virtual `FileBackend` when the filesystem subsystem is enabled).

Read tools with `parallel=True` may run concurrently. Write and exec tools serialize by default. Optional `ResourceAccess` declarations prevent overlapping read/write conflicts on the same resource. Concurrency limit: `Agent(max_tool_concurrency=...)` or `AGENTKIT_MAX_TOOL_CONCURRENCY` env var (default: CPU count).

**Timeouts**: `Agent(tool_timeout_ms=N)` sets an agent-wide execution deadline (env `AGENTKIT_TOOL_TIMEOUT_MS`). A tool may override with a class-level `execution_timeout_ms` attribute; `0` opts out. Default `None` = no timeout (zero-overhead, backward compatible). A timed-out tool returns `is_error=True` with an actionable message; the run continues. Implemented via `asyncio.wait_for` catching `asyncio.TimeoutError` (Python 3.10 safe). `ToolTimeoutError` (`kind="tool_timeout"`, `retryable=True`) is the typed exception for observer/policy use.

**Retry**: `Agent(tool_retry=RetryOptions(max_attempts=..., base_delay_ms=...))` enables opt-in exponential-backoff retry. Gated by scope: read-scope tools retry any exception (idempotent); write/exec tools only retry when the tool sets `retryable = True`. `AbortError` is never retried.

`ToolResult` supports plain `content` plus `summary`, `metadata`, `citations`, `attachments`, `duration_ms`, and `truncated`. The model receives the compact `content`; host apps can use the richer fields for RAG provenance and UI rendering.

### Context (`context/`)

Use `ContextBuilder.build(turn) -> ContextBuildResult` for RAG, memory recall, ephemeral system blocks, budget trimming, and request-scoped tool selection. Builder output is appended only to the provider request and does not mutate `session.provider_view`. The legacy `context_hooks.py` API has been removed.

### Memory (`memory/`)

`MemoryStore` is a protocol for app-owned memory backends. Core includes `MemoryItem`, `MemorySearchResult`, `InMemoryKeywordMemoryStore`, `SqliteMemoryStore`, `MemoryContextBuilder`, `MemorySearchTool`, and `MemoryUpsertTool`. Do not add vector DB or embedding dependencies to core; adapters should implement the protocol or live in examples/recipes.

`TieredMemoryStore` is itself a `MemoryStore` — a deterministic router/merge over three sub-stores (`working`/`episodic`/`semantic`), routing writes by `item.metadata["tier"]` (default `working`) and fanning `search()` across all tiers, then merging by the canonical `(score, item.id)` key and slicing to the global `limit`. Optional `tier_limits` is a **hard per-tier cap** applied before the merge; leave it unset for a pure global top-N. `MemoryContextBuilder(group_by_tier=True)` renders results under tier subheadings (default output is byte-identical). An optional `PostgresMemoryStore` adapter (`[postgres]` extra) mirrors `SqliteMemoryStore`'s full-scan keyword search.

### Virtual Filesystem (`filesystem/`)

`FileBackend` is a duck-typed protocol for a virtual, session-scoped filesystem that is separate from the real `cwd` on disk. Four implementations ship: `StateFileBackend` (in-memory, per-session default), `DiskFileBackend` (real files sandboxed under a root, default `.linch/offload`), `SqliteFileBackend` (persistent across sessions), and `CompositeFileBackend` (routes paths by prefix, e.g. `/memories/` → `SqliteFileBackend`).

**Auto-offload**: `Agent(result_offload=OffloadConfig())` enables automatic offloading of tool results that exceed `threshold_tokens` (default 20,000). The scheduler calls `maybe_offload()` at the single result chokepoint in `_execute_one` (`scheduler.py`). The full payload is written to the backend; `ToolResult.content` is replaced with a preview + path hint before the `ToolResultBlock` enters `provider_view`. The full `ToolResult` still travels on `ToolCallEndEvent.tool_result` for observers. `maybe_offload` never raises — a backend write failure silently returns the original result.

**Four tools** register automatically when a backend is configured (`Agent(filesystem=...)` or `Agent(result_offload=...)`): `ls`, `read_file` (supports `offset`/`limit` line windowing), `write_file`, `edit_file`. These are in `OffloadConfig.skip_tools` by default so reading back a large file cannot trigger a recursive re-offload. A filesystem-aware system-prompt block is also added automatically.

`FeatureFlags(filesystem=False)` disables the subsystem even when a backend is passed.

### Permissions (`permissions/`)

`PermissionEngine` evaluates each tool call against configured rules before execution. Modes: `"default"` (prompt user), `"acceptEdits"` (auto-allow file edits), `"skip-dangerous"` (allow all). Rules: `ToolRule`, `BashRule`, `PathRule`. `BashRule` matches via fnmatch-glob and token-prefix (no substring matching); `PathRule` translates globs to anchored regexes where `*`/`?` do **not** cross `/` and `**` does (`permissions/rules.py`). When a tool call is not auto-approved, a `PermissionRequestEvent` is emitted and the loop pauses until the caller responds.

**Durable HITL**: resolved decisions are persisted into the run checkpoint (`RunCheckpoint.permission_decisions`, keyed by `permission_decision_key(tool_name, input)` in `permissions/keys.py` — stable across provider calls, unlike `tool_use_id`). On resume the scheduler replays a stored allow/deny instead of re-invoking the callback (Seam A); only explicit allow/deny are persisted (Seam B) — abort/callback-failure denials are not. A corrupt stored decision falls through to a fresh prompt.

### Sessions & Storage (`sessions/`)

`SessionStore` is a protocol. Implementations: `InMemorySessionStore` (ephemeral) and `SqliteSessionStore` (persistent). The store also handles task management (`Task`, `TaskPatch`, status tracking) used by the `TaskCreate/List/Get/Update` tools.

`run_store.py` provides `SqliteRunStore` and `RunCheckpoint` for durable run checkpointing, enabling deep-agent runs to resume across process restarts.

### Deep agent preset (`deep_agent/`)

`create_deep_agent(*, model, durable, coordinator, cwd, ...)` is a factory in `deep_agent/factory.py` that assembles a fully-configured `Agent` with task tools, a built-in subagent roster (researcher, planner, implementer defined in `deep_agent/subagents.py` as `DEEP_AGENT_SUBAGENTS`), durable SQLite stores, a `/memories` filesystem partition, and a deepened system prompt.

- **`coordinator=True`** — strips heavy tools (`Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Read`) from the parent agent, injects `COORDINATOR_SYSTEM_PROMPT` (from `deep_agent/prompts.py`), and registers `TaskStopTool`. Workers still receive full tool access.
- **`durable=True`** — sets `SqliteSessionStore`, `SqliteRunStore`, and `CompositeFileBackend` with a persistent `/memories` partition backed by `SqliteFileBackend`, enabling cross-process resume.
- `DEEP_AGENT_SYSTEM_PROMPT` and `COORDINATOR_SYSTEM_PROMPT` live in `deep_agent/prompts.py`.
- `DEEP_AGENT_SUBAGENTS` in `deep_agent/subagents.py` defines researcher, planner, and implementer subagent definitions used by default.

### MCP Integration (`mcp/`)

`connect_mcp_servers()` connects to external MCP servers (stdio or HTTP) and returns an `McpConnection` that exposes MCP tools as Linch tools. MCP tool names are normalized via `mcp/naming.py`.

### Skills & Subagents (`skills/`, `subagents/`)

**Skills** are slash-commands defined as `SKILL.md` files (YAML frontmatter + markdown body) loaded from `.linch/skills/*/SKILL.md`. The skill system supports argument substitution and system-reminder injection.

**Subagents** are specialized agent roles defined in `.linch/agents.yaml`. The subagent registry resolves agent definitions; `runner.py` executes them with their own tool overlays and prompts.

**Worker lifecycle and fork/continue**: `SubagentTool` passes `retain=True` in `RunSubagentArgs` so child sessions remain alive in `agent._sessions` after the initial run. `_drive_child` is a shared helper used by both `run_subagent` and `continue_subagent` to drive a child session. `SubagentContinueTool` (schema: `{to, message}`) re-engages a retained worker by worker_id or display_name, resuming with its full prior `provider_view`. `TaskStopTool` (schema: `{task_id, reason}`) cancels a running background worker task; the handle remains continuable. `WorkerHandle` (`subagents/workers.py`) is a dataclass tracking `worker_id`, `child_session_id`, `display_name`, `definition`, `status`, `task`, and `last_result_text` for each live worker. Parent session exposes `session.workers: dict[str, WorkerHandle]`.

**Background workers**: `SubagentTool` with `run_in_background=True` spawns `asyncio.create_task` and returns an ack immediately without blocking the turn. On completion the worker appends a `<task-notification>` XML `Message` to `session.pending_notifications`. The next `session.run()` call drains all pending notifications as `UserEvent`s at the top of the turn via `_drain_pending_notifications` in `loop.py`. `session.abort()` cancels all running background worker tasks; `agent.close()` also cancels them and clears `_sessions`.

### Compaction (`compaction.py`)

When the provider's context window approaches its limit, the compaction strategy summarizes old messages to free space. This is transparent to the caller; a `CompactionEvent` is emitted.

### Observability (`observability/`)

`RunObserver` is a vendor-neutral protocol with nine span-hook methods (`on_run_start/end`, `on_turn_start/end`, `on_provider_call_start/end`, `on_tool_start/end`) plus an `on_event` catch-all. Methods may be sync or async.

`ObserverDispatcher` fans out hook calls to a list of observers, awaits async results, and swallows exceptions — a faulty observer never crashes a run. Zero-overhead when no observers are attached.

Stdlib reference observers: `LoggingObserver` (one log line per span) and `SpanCollector` (in-memory span list for tests). `OpenTelemetryObserver` is the production integration point, behind the optional `[otel]` extra (lazily imported, `pip install 'linch[otel]'`). Langfuse, LangSmith, Honeycomb, and Datadog are all reached via the OTel adapter — no vendor-specific code in core.

Observers are attached via `Agent(observers=[...])` and accessed as `agent.observers`.

### Evals (`evals/`)

A lightweight, deterministic harness for grading agent behavior offline. `ScriptedProvider` (with `TextTurn`/`ToolUseTurn`) replays a fixed turn sequence without a live model. `run_eval(agent_factory, cases, scorers)` runs an agent over a list of `EvalCase`s and returns `EvalResult`/`CaseResult` with per-scorer pass/fail/None. Built-in scorers (`evals/scorers.py`) each return `True`/`False`/`None`: `text_contains`, `tool_called`, `schema_valid`, `cost_under`, plus long-run scorers `context_selected_tool`, `context_not_trimmed`, `context_metadata_contains`, `memory_recalled`, `recovery_succeeded`, and `run_completed`. Each case session is popped from `agent._sessions` and aborted in a `finally`, so a long eval run does not leak sessions.

### Run reports (`reports.py`)

`build_run_report(events, run=None)` folds an event stream (live `Event`s or persisted `StoredRunEvent`s) into a `RunReport` dataclass: tool calls, permission requests, context builds, loop guards, errors, usage, final result, checkpoint, and a `long_run` summary (selected-tool counts, peak context tokens, memory tier/namespace/citation rollups) plus a flat `timeline`. `load_run_report(store, run_id)` reconstructs one from a `RunStore`. Pure read model — it never mutates session state.

## Key design constraints

- All async — no blocking I/O anywhere in the core loop.
- `provider_view` vs `full_history` are kept separately; only `provider_view` is sent to the LLM. Compaction modifies `provider_view` only.
- Tool protocol is duck-typed — avoid inheriting from a base class when adding tools; implement the protocol attributes directly.
- The loop continues as long as the response contains tool calls; it stops when the model returns a text-only response (or hits a stop condition).
- No vendor observability stack in core — observability backends (Langfuse, LangSmith, etc.) are reached via the OpenTelemetry seam, never as direct dependencies.
- Tool timeouts default to `None` (off) and use `asyncio.wait_for` + `asyncio.TimeoutError` (not the 3.11+ unified builtin) to stay Python 3.10 compatible. Timeouts convert to `is_error=True` results; they never raise out of `_execute_one` so parallel-lane siblings are unaffected.
- Tool retry is side-effect gated: read-scope tools only, or tools that explicitly set `retryable = True`. Write/exec tools are never retried by default.
- Virtual filesystem is opt-in (`Agent(filesystem=...)` or `Agent(result_offload=...)`); zero overhead when unset. `DiskFileBackend` defaults to `.linch/offload` (gitignored). `maybe_offload` is a no-op on error results and filesystem-tool results. A backend write failure silently returns the original result — storage errors never crash a run.
- Background workers use `asyncio.create_task` detached from the current turn; `session.abort()` cancels them and `agent.close()` also cancels and clears `_sessions`. `_cancel_background_workers` is called in both `except AbortError` and `except Exception` branches so orphaned tasks never write into a dead session.
