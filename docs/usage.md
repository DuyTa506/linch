# Using AgentKit in Your Project

This guide shows how to install AgentKit, initialise an agent, and build
workflows for any domain — not just software engineering.

---

## Installation

```bash
# From the repo (development)
pip install -e /path/to/agent_kit

# With all optional extras
pip install -e "/path/to/agent_kit[mcp,anthropic]"
```

Once published, `pip install agent-kit` will work directly.

---

## Minimum working agent

```python
import asyncio
from agent_kit import Agent
from agent_kit.sessions import InMemorySessionStore

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

---

## Key configuration knobs

### Model & provider

```python
from agent_kit.providers import OpenAIResponsesProvider, OpenAIChatCompletionsProvider
from agent_kit.openai_responses import OpenAIOptions

# Default: OpenAI Responses API (o-series, gpt-5, …)
agent = Agent(model="gpt-5", openai_api_key="sk-...")

# Chat Completions (gpt-4o, gpt-4-turbo, …)
agent = Agent(
    model="gpt-4o",
    provider=OpenAIChatCompletionsProvider(options),
)

# Custom base URL (Azure, local proxy, …)
agent = Agent(
    model="my-model",
    openai=OpenAIOptions(api_key="...", base_url="https://..."),
)
```

### Session store

```python
from agent_kit.sessions import InMemorySessionStore, SqliteSessionStore
from pathlib import Path

# Ephemeral (tests, single-request workers)
store = InMemorySessionStore()

# Persistent (keep history across restarts)
store = SqliteSessionStore(Path("~/.myapp/sessions.db").expanduser())
```

### Feature flags (skip subsystems you don't use)

```python
from agent_kit.config import FeatureFlags

agent = Agent(
    ...
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
)
```

### System prompt control

```python
from agent_kit.config import SystemPromptConfig

# Append instructions to the built-in AgentKit prompt
agent = Agent(..., system_prompt="Always reply in formal English.")

# Replace the entire SWE identity with your own
agent = Agent(
    ...,
    system_prompt_config=SystemPromptConfig(
        replace_defaults=True,
        append="You are a financial analyst. Only discuss stocks and bonds.",
    ),
)
```

### Custom tools

```python
from agent_kit.tools.base import ToolContext, ToolResult
from agent_kit.tools.registry import empty_tools, tools_from_defaults

class MyTool:
    name = "search_kb"
    description = "Search the internal knowledge base."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"          # "read" | "write" | "exec"
    parallel_safe = True    # can run concurrently with other parallel tools

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return raw

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        results = await ctx.deps.kb.search(input["query"])  # use deps
        return ToolResult(content=results, summary=f"search_kb({input['query'][:40]})")

    def summarize(self, input: dict) -> str:
        return f"search_kb({input.get('query','')[:40]})"

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(MyTool()))

# SWE tools minus Bash, plus custom
from agent_kit.tools.registry import tools_from_defaults
registry = tools_from_defaults(exclude={"Bash"}, extra=[MyTool()])
agent = Agent(..., tools=registry)
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
from agent_kit import RunOptions
async for event in session.run("...", RunOptions(deps=tenant_db)):
    ...
```

### Permissions

```python
from agent_kit.permissions import ToolRule, PathRule, BashRule

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
from agent_kit.types import OutputSchema

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

### Per-turn context injection (RAG)

```python
from agent_kit.context_hooks import ContextInjector, TurnContext, prune_tagged
from agent_kit.types import Message, TextBlock

TAG = "[[ctx]]"

class MyInjector:
    async def before_turn(self, ctx: TurnContext) -> None:
        prune_tagged(ctx.provider_view, TAG)   # remove last turn's injections
        docs = await ctx.deps.search(last_query(ctx))
        if docs:
            ctx.provider_view.append(Message(
                role="user",
                content=[TextBlock(text=f"{TAG}\n{docs}")],
            ))

agent = Agent(..., context_injectors=[MyInjector()], deps=my_store)
```

---

## See `examples/` for runnable code

| File | What it shows |
|------|---------------|
| `01_custom_tools.py` | 5 tool patterns: read, write, exec, parallel, with deps |
| `02_custom_permissions.py` | All permission modes and rule types |
| `03_system_prompts.py` | append, replace, per-session override, persona patterns |
| `04_structured_output.py` | OutputSchema, final_tool_name, JSON extraction |
| `05_context_injection.py` | RAG per-turn, sliding-window context, per-turn schema inject |
| `06_multi_session.py` | Web-app pattern: one Agent, many users, shared deps |
| `07_event_streaming.py` | Consuming events for SSE, WebSocket, CLI progress |
| `minimal_agent.py` | Smallest possible agent |
| `interactive_cli.py` | Interactive REPL |
