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
3. **run_loop / stream_turn** (`loop.py`) — the main agent loop. Each turn: build user message → call `provider.stream()` → collect text/tool-use blocks → check permissions → execute tools (via `scheduler.py`) → emit events → repeat if more tool calls; stop on text-only response.
4. **Events** (`events.py`) — all communication from the loop is through events: `UserEvent`, `AssistantEvent`, `ToolCallStartEvent`, `ToolCallEndEvent`, `PermissionRequestEvent`, `UsageEvent`, `ResultEvent`, `ErrorEvent`, `CompactionEvent`, and skill/subagent events.

### Providers (`providers/`)

Abstract interface (`BaseProvider`) with two methods: `context_window(model)` and `stream(req) → AsyncIterator[StreamEvent]`. Implementations:
- `OpenAIChatCompletionsProvider` — standard OpenAI Chat API
- `OpenAIResponsesProvider` — OpenAI o1/o3 Responses API (with reasoning tokens)
- `AnthropicProvider` — Anthropic Claude
- `RetryProvider` — wraps any provider with exponential-backoff retry

### Tools (`tools/`)

Tools are **protocols** (duck-typed), not subclasses. Each tool has: `name`, `description`, `input_schema`, `scope`, `parallel_safe`, and methods `validate()`, `execute()`, `summarize()`. Built-ins: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`. The `ToolRegistry` holds available tools; `default_tools()` returns the standard set.

`ToolContext` is passed to every `execute()` call and provides: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`.

Tools with `parallel_safe=True` may run concurrently. Concurrency limit: `AGENTKIT_MAX_TOOL_CONCURRENCY` env var (default: CPU count).

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

## Key design constraints

- All async — no blocking I/O anywhere in the core loop.
- `provider_view` vs `full_history` are kept separately; only `provider_view` is sent to the LLM. Compaction modifies `provider_view` only.
- Tool protocol is duck-typed — avoid inheriting from a base class when adding tools; implement the protocol attributes directly.
- The loop continues as long as the response contains tool calls; it stops when the model returns a text-only response (or hits a stop condition).
