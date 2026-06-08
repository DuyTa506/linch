# Using Linch in Your Project

This guide shows how to install Linch, initialise an agent, and build
workflows for any domain — not just software engineering.

---

## Installation

```bash
# From the repo (development)
pip install -e /path/to/linch

# With common optional extras
pip install -e "/path/to/linch[mcp,anthropic,gemini,postgres]"
```

Once published, `pip install linch` will work directly.

---

## Minimum working agent

```python
import asyncio
from linch import Agent
from linch.sessions import InMemorySessionStore

agent = Agent(
    model="gpt-5",                    # or any supported model
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},     # auto-approve all tool calls
)

async def main():
    session = await agent.session()
    async for event in session.run("What day is it?"):
        if event.type == "result":
            print(event.final_text)

asyncio.run(main())
```

---

## Core concepts

```
Agent ──── long-lived config (model, tools, permissions, system prompt, deps)
  └── Session ── conversation state (messages, run_deps)
        └── session.run(prompt) ── AsyncIterator[Event]
```

**`Agent`** is created once (per model/configuration) and reused across many
conversations.  
**`Session`** is one conversation thread. A user in a web app gets their own
session but shares the same Agent.  
**`session.run()`** returns an async iterator of typed events — stream them to
your UI as they arrive.

---

## Event types

```python
async for event in session.run("hello"):
    match event.type:
        case "system":    # run started — model, tools, cwd
        case "user":      # user message appended
        case "assistant": # full assistant turn (final)
        case "partial_assistant":  # streaming text/thinking delta
        case "tool_call_start":   # tool about to run
        case "tool_call_end":     # tool finished, has .result
        case "permission_request": # user approval needed (mode="default")
        case "usage":     # token counts for this turn
        case "result":    # run finished — .subtype in ("success","error","aborted")
        case "error":     # provider/tool error details
        case "compaction": # context was summarised
```

`ResultEvent` is always the last event. Check `event.subtype` and
`event.final_text` (or `event.structured_output` when using an
`OutputSchema`).

Usage and result events include optional USD cost fields when the model exists
in `linch.pricing`'s table:

```python
async for event in session.run("Summarize this thread."):
    if event.type == "usage":
        print(event.cost_usd, event.cumulative_cost_usd)
    elif event.type == "result":
        print(event.total_cost_usd)
```

Unknown model IDs report `None` for cost rather than pretending the call is
free. Use `linch.pricing.cost_usd(usage, model, table=...)` with a custom table
for private or self-hosted models.

---

## Key configuration knobs

### Model & provider

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

**Reading thinking events** (any provider that emits `reasoning_content` or Anthropic thinking):

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

### Session store

```python
from linch.sessions import InMemorySessionStore, SqliteSessionStore
from pathlib import Path

# Ephemeral (tests, single-request workers)
store = InMemorySessionStore()

# Persistent (keep history across restarts)
store = SqliteSessionStore(Path("~/.myapp/sessions.db").expanduser())
```

### Feature flags (skip subsystems you don't use)

```python
from linch.config import FeatureFlags

agent = Agent(
    ...
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    # also: filesystem=False to disable the virtual filesystem subsystem
)
```

### Skills

Skills are prompt workflows exposed through the `Skill` tool when
`FeatureFlags(skills=True)`.

Linch includes a built-in `verify` skill:

```text
Skill({"skill": "verify", "args": "focus on billing workflow"})
```

`verify` asks the model to plan and run evidence-based checks for completed
work, then end with `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`.
It is domain-agnostic: use it for software changes, data workflows, documents,
configuration, or other concrete deliverables.

Project skills live at `.linch/skills/<name>/SKILL.md`. A project skill
named `verify` overrides the built-in.

### Compaction

```python
from linch import Agent, DetailedCompaction

# DefaultCompaction remains the default. DetailedCompaction is opt-in and uses
# a continuation-safe summary structure for long-running sessions.
agent = Agent(
    model="gpt-5",
    compaction=DetailedCompaction(),
)
```

### System prompt control

```python
from linch.config import SystemPromptConfig, SystemPromptSection

# Append instructions to the built-in Linch prompt
agent = Agent(..., system_prompt="Always reply in formal English.")

# Replace the entire SWE identity with your own
agent = Agent(
    ...,
    system_prompt_config=SystemPromptConfig(
        replace_defaults=True,
        append="You are a financial analyst. Only discuss stocks and bonds.",
    ),
)

# Add reusable prompt sections without replacing the defaults
agent = Agent(
    ...,
    system_prompt_config=SystemPromptConfig(
        sections=[
            SystemPromptSection(
                name="domain-policy",
                text="When handling invoices, preserve source document IDs in every answer.",
                placement="after_defaults",
            )
        ]
    ),
)
```

### Custom tools

```python
from linch.tools.base import ResourceAccess, ToolContext, ToolResult
from linch.tools.registry import empty_tools, tools_from_defaults

class MyTool:
    name = "search_kb"
    description = "Search the internal knowledge base."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"          # "read" | "write" | "exec"
    parallel = True         # can run concurrently when scope is read

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return raw

    def resources(self, input: dict) -> list[ResourceAccess]:
        return [ResourceAccess(resource="kb:default", mode="read")]

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        results = await ctx.deps.kb.search(input["query"])  # use deps
        return ToolResult(
            content=results,
            summary=f"search_kb({input['query'][:40]})",
            metadata={"query": input["query"]},
        )

    def summarize(self, input: dict) -> str:
        return f"search_kb({input.get('query','')[:40]})"

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(MyTool()))

# SWE tools minus Bash, plus custom
from linch.tools.registry import tools_from_defaults
registry = tools_from_defaults(exclude={"Bash"}, extra=[MyTool()])
agent = Agent(..., tools=registry)
```

The scheduler runs independent read/search tools in parallel, serializes
write/exec tools, respects `Agent(max_tool_concurrency=...)`, and uses optional
`ResourceAccess` declarations to avoid read/write conflicts on the same file,
database, index, or other host-defined resource.

**Timeouts** — set `Agent(tool_timeout_ms=N)` (or env `AGENTKIT_TOOL_TIMEOUT_MS`)
for an agent-wide deadline. A timed-out tool returns `is_error=True` and the run
continues. Per-tool override: set `execution_timeout_ms` as a class attribute on
the tool; `0` opts out of the agent default.

**Retry** — pass `Agent(tool_retry=RetryOptions(max_attempts=3, base_delay_ms=50))`
to retry on transient failures. Read-scope tools retry any exception (they are
idempotent); write/exec tools only retry when the tool declares `retryable = True`.
`AbortError` is never retried.

**Bash execution backend** — `Bash` uses `LocalBackend` by default. To run Bash
through an injected backend, pass `execution_backend=...` to `Agent`. The agent
only replaces an existing `Bash` tool; it does not add shell access to a custom
registry that deliberately omits `Bash`.

```python
from linch.tools.execution import DockerBackend

agent = Agent(
    ...,
    execution_backend=DockerBackend(image="python:3.12-slim"),
)
```

### Dependencies (shared app state)

```python
# Anything: a DB connection, vector store, API client, config dict
agent = Agent(..., deps={"db": my_db, "vector_store": vs})

# Access inside any tool:
async def execute(self, input, ctx: ToolContext) -> ToolResult:
    results = await ctx.deps["vector_store"].search(input["query"])
    ...

# Override per-run (e.g. tenant-specific connection)
from linch import RunOptions
async for event in session.run("...", RunOptions(deps=tenant_db)):
    ...
```

### Permissions

```python
from linch.permissions import ToolRule, PathRule, BashRule

agent = Agent(
    ...,
    permissions={
        "mode": "acceptEdits",          # auto-allow file edits; ask for Bash
        "rules": [
            ToolRule(tool="Bash", decision="deny"),              # block Bash entirely
            PathRule(paths=["/secrets/**"], decision="deny"),    # block secret paths
            BashRule(patterns=["rm -rf*"], decision="deny"),     # block dangerous commands
        ],
        "canUseTool": my_approval_callback,   # async or sync
    },
)
```

### Structured output

```python
from linch.types import OutputSchema

schema = OutputSchema(
    name="invoice",
    schema={
        "type": "object",
        "properties": {
            "total": {"type": "number"},
            "line_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["total", "line_items"],
        "additionalProperties": False,
    },
    strict=True,
)

agent = Agent(..., output_schema=schema)
# result.structured_output → {"total": 42.50, "line_items": [...]}
```

### Per-turn context building (RAG)

```python
from linch.context import ContextBudget, ContextBuildResult
from linch.types import Message, TextBlock

TAG = "[[ctx]]"

class MyContextBuilder:
    async def build(self, turn) -> ContextBuildResult:
        docs = await turn.deps.search(last_query(turn.messages))
        if not docs:
            return ContextBuildResult()
        return ContextBuildResult(
            messages=[
                Message(role="user", content=[TextBlock(text=f"{TAG}\n{docs}")])
            ],
            budget=ContextBudget(max_tokens=800),
            metadata={"source": "my_store"},
        )

agent = Agent(..., context_builder=MyContextBuilder(), deps=my_store)
```

### Memory and RAG primitives

```python
from linch import Agent
from linch.memory import (
    InMemoryKeywordMemoryStore,
    MemoryContextBuilder,
    MemoryItem,
    MemorySearchTool,
    TieredMemoryStore,
)
from linch.tools.registry import empty_tools

store = InMemoryKeywordMemoryStore()
await store.upsert([
    MemoryItem(id="m1", content="ToolResult can carry citations.", namespace="docs")
])

agent = Agent(
    ...,
    deps=store,
    context_builder=MemoryContextBuilder(namespace="docs", max_tokens=800),
    tools=empty_tools(MemorySearchTool(namespace="docs")),
)
```

For long-running, multi-session, user-oriented agents, wrap stores with
`TieredMemoryStore` so working, episodic, and semantic memories are routed and
ranked separately:

```python
store = TieredMemoryStore()
await store.upsert([
    MemoryItem(
        id="pref-1",
        content="The user prefers concise status updates.",
        namespace="user:42",
        metadata={"tier": "semantic"},
    )
])
```

Core includes `MemoryStore` protocols, cooperative in-memory keyword memory,
SQLite memory, optional Postgres memory via `pip install 'linch[postgres]'`,
tiered memory, and memory search/upsert tools. Vector databases and embedding
models stay in the host app or an adapter.

### Virtual filesystem and large-result offloading

Variable-length tool results (RAG, web search, file dumps) are the #1 source of
context-window blowup. The virtual filesystem subsystem handles this automatically:
when a tool result exceeds a token threshold, the scheduler writes the full
payload to a `FileBackend` and replaces what the model sees with a short preview
plus a path reference. The model pulls back only what it needs via `read_file`.

**On by default.** Every `Agent()` enables offloading with an ephemeral
`StateFileBackend`. The threshold is derived automatically from the model's context
window (`threshold_fraction=0.1` → 10 % of the context window). A 128 k-token model
offloads results above ~12 800 tokens; a 200 k model above ~20 000 tokens. No
configuration required unless you want to change the backend or tune the threshold.

```python
# Default — ephemeral in-memory backend, threshold = 10 % of context window
agent = Agent(...)   # offload is already on

# Persist offloaded files under .linch/offload (inspectable, gitignored)
from linch.filesystem import DiskFileBackend, OffloadConfig
agent = Agent(
    ...,
    filesystem=DiskFileBackend(root=".linch/offload"),
)

# Tune the threshold or fraction explicitly
agent = Agent(
    ...,
    result_offload=OffloadConfig(threshold_tokens=5_000),   # hard override
    # or:
    result_offload=OffloadConfig(threshold_fraction=0.05),  # 5 % of context
)

# Ephemeral scratch + persistent /memories/ across sessions
from linch.filesystem import CompositeFileBackend, SqliteFileBackend, StateFileBackend
agent = Agent(
    ...,
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": SqliteFileBackend(".linch/memories.db")},
    ),
)

# Disable entirely
agent = Agent(..., result_offload=None)
# or: features=FeatureFlags(filesystem=False)
```

When the subsystem is active, four tools are registered automatically:

| Tool | Description |
|---|---|
| `ls(prefix?)` | List files in the virtual filesystem |
| `read_file(path, offset?, limit?)` | Read a file, optionally windowed by line range |
| `write_file(path, content)` | Write a scratchpad note or intermediate result |
| `edit_file(path, old_string, new_string, replace_all?)` | Edit an existing file |

The model is informed about these tools and the offload convention via a system-prompt
block added automatically. The full original content is always preserved in the backend
and on `ToolCallEndEvent.tool_result` for observers — only what enters `provider_view`
(conversation history) is replaced by the preview.

**`OffloadConfig` options:**

```python
OffloadConfig(
    enabled=True,               # master switch
    threshold_tokens=None,      # None = derive from context window (recommended)
    threshold_fraction=0.1,     # fraction used when threshold_tokens is None (10 %)
    preview_lines=10,
    path_prefix="/offload",     # virtual directory for auto-offloaded files
    skip_tools=frozenset({"read_file", "write_file", "edit_file", "ls"}),
)
```

`threshold_tokens` is resolved once at `Agent.__init__` time from
`int(context_window * threshold_fraction)`.  Pass an explicit integer to override
(e.g. `threshold_tokens=5_000`).  The filesystem tools are always excluded from
offloading so reading a large file back does not recursively re-offload it.

---

## Deep agent preset

`create_deep_agent()` is a convenience factory that wires up a multi-agent
configuration with a single call.

### `create_deep_agent()`

```python
from linch import create_deep_agent
from linch.providers import OpenAIChatCompletionsProvider

agent = create_deep_agent(
    model="deepseek-v4-pro",
    provider=OpenAIChatCompletionsProvider(...),  # any provider
    cwd=".",                           # workspace root
    durable=True,                      # SQLite session + run + /memories stores
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
```

`durable=True` sets up three persistent stores: `SqliteSessionStore`,
`SqliteRunStore`, and a `CompositeFileBackend` with a persistent `/memories`
partition (SQLite-backed). Everything else in the virtual filesystem is
ephemeral (`StateFileBackend`). With `durable=False` all stores are in-memory.

### Background workers

Spawn a worker in the background by passing `run_in_background=True` to the
`Subagent` tool. The turn returns immediately with an ack; a
`<task-notification>` is injected at the top of the next turn once the worker
finishes.

```python
# Turn 1 — spawn in background (returns immediately with ack)
async for event in session.run(
    "Use Subagent with subagent_type='researcher' and run_in_background=True. "
    "Task: summarise Python asyncio.gather in 2 sentences."
):
    if event.type == "result":
        print(event.final_text)  # "Worker agent-xxxx started in background."

# Wait for worker (optional — turn 2 will receive the notification even without this)
for handle in session.workers.values():
    if handle.task and not handle.task.done():
        await handle.task

# Turn 2 — <task-notification> is drained automatically at the top of this turn
async for event in session.run("Summarise what the background researcher found."):
    if event.type == "result":
        print(event.final_text)
```

### Fork/continue

Every `Subagent` result includes a `[Worker ID: agent-xxxx]` suffix so the
coordinator can re-engage the same worker with its full context intact using
`SubagentContinue`.

```python
# Turn 1 — spawn foreground worker
async for event in session.run(
    "Use Subagent(subagent_type='researcher') to explain asyncio.gather in 1 sentence."
):
    if event.type == "result":
        print(event.final_text)   # includes "[Worker ID: agent-a1b2]"

# Turn 2 — continue the same worker with its full context
async for event in session.run(
    "Use SubagentContinue(to='agent-a1b2', message='Give a one-line code example.')"
):
    if event.type == "result":
        print(event.final_text)
```

`session.workers` is a `dict[str, WorkerHandle]`. Each handle exposes
`handle.child_session_id`, `handle.status`, and `handle.last_result_text`.

### Coordinator mode

```python
agent = create_deep_agent(
    model="...",
    coordinator=True,          # parent orchestrates only
    durable=False,
    permissions={"mode": "skip-dangerous"},
)
# Parent has no Edit/Write/Bash/Grep/Glob/Read — only Subagent/SubagentContinue/TaskStop + task tools
# Workers receive full tool access via SubagentTool → build_child_tools
```

To stop a running background worker: `TaskStop(task_id='agent-xxxx')`. The
handle stays in `session.workers` so it can be continued later with
`SubagentContinue`.

---

## See `examples/` for runnable code

Examples are organized by subsystem. Local demos (marked *local*) run without
a live API key.

**`core/`**

| File | What it shows |
|------|---------------|
| `core/minimal_agent.py` | Smallest possible agent |
| `core/coding_agent.py` | Full SWE agent — tools_from_defaults, BashRule/PathRule safety fence, LoopGuard, multi-turn |
| `core/reading_agent.py` | Read-only codebase Q&A — exclude Write/Edit/Bash, PathRule, custom reviewer persona |
| `core/chat_agent.py` | Pure conversation agent — no tools, custom domain, structured JSON output via ContextBuilder injection |
| `core/custom_permissions.py` | All permission modes and rule types |
| `core/system_prompts.py` | append, replace, per-session override, persona patterns |
| `core/structured_output.py` | OutputSchema, final_tool_name, JSON extraction |
| `core/event_streaming.py` | Consuming events for SSE, WebSocket, CLI progress |
| `core/multi_session.py` | Web-app pattern: one Agent, many users, shared deps |
| `core/loop_guard_agent.py` | LoopGuard — identical-call and failure-streak detection |
| `core/interactive_cli.py` | Interactive REPL |
| `core/deep_agent_resume.py` | `create_deep_agent` — 4 demos: planning + /memories, background worker + notification, fork/continue, coordinator mode |

**`tools/`** — *local demos available*

| File | What it shows |
|------|---------------|
| `tools/custom_tools.py` | 5 tool patterns: read, write, exec, parallel, with deps |
| `tools/parallel_search_agent.py` | Scheduler V2: parallel search, resources, concurrency cap |
| `tools/runtime_tools.py` | Runtime registry add/remove/replace/select and schema export |
| `tools/tool_reliability_agent.py` | Timeout, per-tool opt-out (`execution_timeout_ms=0`), `RetryOptions` |
| `tools/rag_tools.py` | RAG tool suite: hybrid_search, keyword_search, graph_search, web_search |
| `tools/filesystem_offload.py` | Virtual filesystem backends, auto-offload of large results (*runs offline*) |

**`context/`** — *local demos available*

| File | What it shows |
|------|---------------|
| `context/context_injection.py` | ContextBuilder patterns: RAG per-turn, budget, selected tools |
| `context/rag_context_builder.py` | First-class ContextBuilder RAG with metadata and budget reporting |

**`memory/`** — *local demos available*

| File | What it shows |
|------|---------------|
| `memory/memory_agent.py` | Core memory primitives with search/upsert tools and citations |
| `memory/sqlite_memory_agent.py` | SqliteMemoryStore — persistent memory, round-trip, upsert update |

**`observability/`**

| File | What it shows |
|------|---------------|
| `observability/observability_agent.py` | LoggingObserver + optional OpenTelemetryObserver |
| `observability/custom_observer.py` | BaseObserver subclass: latency tracking, error counts per tool |

**`providers/`**

| File | What it shows |
|------|---------------|
| `providers/openai_agent.py` | OpenAIChatCompletionsProvider — basic Q&A, thinking events, tool use, thinking + tool use, structured output, multi-turn |
| `providers/anthropic_agent.py` | AnthropicProvider — basic Q&A, extended thinking (with `PartialAssistantEvent`), prompt caching |
| `providers/deepseek_agent.py` | DeepSeek via both OpenAI-compatible and Anthropic-compatible endpoints — thinking, tool use, thinking + tool use, multi-turn |

**`integrations/`** — *local demo available*

| File | What it shows |
|------|---------------|
| `integrations/subagent_coordinator.py` | Agent definition files, tool-filtered subagents, SubagentEvent |
| `integrations/multi_agent_isolation.py` | Context isolation: child work never enters parent context; sequential pipeline; parallel analysts; subagent + filesystem offload (*runs offline*) |

Built-in subagents are available without disk definitions. After non-trivial
implementation or workflow changes, ask the model to invoke `Subagent` with
`subagent_type="verification"` and a prompt that includes the original task,
artifacts or files changed, approach taken, and checks you expect it to run.
The verification subagent is restricted to `Read`, `Glob`, `Grep`, and `Bash`
and must end with `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`.
