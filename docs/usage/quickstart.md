# Quickstart

[<- Usage guide](./README.md)

This path is for a new developer who wants to get a Linch agent running first,
then learn the subsystems. Start with the offline script, then switch to a live
provider once the event loop shape is clear.

## 1. Install

From PyPI:

```bash
pip install linch
```

From a local checkout:

```bash
pip install -e '.[dev,mcp,anthropic,gemini]'
```

Linch targets Python 3.10+. Optional extras are intentionally split by feature;
you only need provider extras when you use those providers.

## 2. Run an offline smoke test

This verifies the SDK shape without an API key or network call.

Save as `quickstart_offline.py`:

```python
import asyncio

from linch import Agent
from linch.config import FeatureFlags
from linch.evals import ScriptedProvider, TextTurn
from linch.sessions import InMemorySessionStore


agent = Agent(
    model="demo-model",
    provider=ScriptedProvider([TextTurn(text="Hello from Linch.")]),
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    result_offload=None,
)


async def main() -> None:
    session = await agent.session()
    async for event in session.run("Say hello"):
        if event.type == "result":
            print(event.final_text)


asyncio.run(main())
```

Run it:

```bash
python3 quickstart_offline.py
```

Expected output:

```text
Hello from Linch.
```

## 3. Run with a live provider

For the default OpenAI provider, export an API key:

```bash
export OPENAI_API_KEY="..."
```

Save as `quickstart_live.py`:

```python
import asyncio
import os

from linch import Agent
from linch.sessions import InMemorySessionStore


agent = Agent(
    model="gpt-5",
    openai_api_key=os.environ["OPENAI_API_KEY"],
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},
)


async def main() -> None:
    session = await agent.session()
    async for event in session.run("Explain Linch in one paragraph."):
        if event.type == "result":
            print(event.final_text)


asyncio.run(main())
```

`session.run(...)` is an async event stream. Build CLIs, web sockets, server-sent
events, logs, and progress views from those events instead of waiting for one
blocking return value.

## 4. Add a tool

Tools are plain Python objects. The `@tool` decorator is the shortest path:

```python
from linch import Agent, tool
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools


@tool(description="Return the current status for a service.")
async def service_status(name: str) -> str:
    return f"{name}: healthy"


agent = Agent(
    model="gpt-5",
    tools=empty_tools(service_status),
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},
)
```

For production, replace `skip-dangerous` with explicit permission rules before
you expose write, shell, filesystem, or external-service tools.

## 5. Keep state when the process restarts

Use SQLite stores when conversation history or run resume matters:

```python
from pathlib import Path

from linch import Agent, SqliteRunStore
from linch.sessions import SqliteSessionStore


state = Path(".linch-state")
state.mkdir(exist_ok=True)

agent = Agent(
    model="gpt-5",
    session_store=SqliteSessionStore(state / "sessions.db"),
    run_store=SqliteRunStore(state / "runs.db"),
    permissions={"mode": "skip-dangerous"},
)
```

`session_store` persists conversation state. `run_store` persists checkpoints
and events, which enables durable resume and operational reports.

## 6. Where to go next

| Goal | Read next |
|---|---|
| Embed Linch in a web service | [Production wiring](./production.md) |
| Understand Agent vs Session | [Agent & session](./agent.md) |
| Add custom tools safely | [Tools](./tools.md) |
| Stream events to a UI | [Events](./events.md) |
| Add RAG/context | [Context & memory](./context-and-memory.md) |
| Choose providers | [Providers](./providers.md) |
| Build long-running workers | [Outer loop runner](./loop-runner.md) |
| Validate extension adapters | [Extending](./extending.md) |
| Find runnable examples | [Examples](./examples.md) |
