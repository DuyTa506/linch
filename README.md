# linch

> **靈** (*linh*) · *cinch*
>
> *靈者，簡之道也。機動而萬事易。*
> *Linh giả, giản chi đạo dã. Cơ động nhi vạn sự dị.*
> *"Spirit is the way of simplicity. When the mechanism stirs, ten thousand things become easy."*

**linch** is a Python SDK for embedding a software engineering agent loop in your application — async-first, event-driven, and provider-agnostic, designed as a **harnessing framework** you build domain workflows on top of.

Core capabilities: streaming events, resource-aware parallel tool scheduling,
per-tool timeout + opt-in retry, first-class context building (RAG), memory/RAG
primitives, **virtual filesystem with automatic large-result offloading**, structured
output, rich tool results with citations/metadata, runtime tool registries,
permission engine, MCP/skills/subagents, and pluggable providers.

## Tên gọi · Etymology

**linch** blends two ideas:

| | |
|---|---|
| **靈** `linh` | In classical Sino-Vietnamese (*chữ Nho*), 靈 carries the meaning of *spirit*, *effortless intelligence*, and *vital agility* — attributed to things that work with invisible precision. The character unites **雨** (heaven, rain above) with **巫** (the shaman — connector between worlds): the unseen force that makes the mechanism live. |
| **cinch** | A saddle *harness* piece; English slang for *easy, simple, sure*. |

A *linch* is small — a single pin — but it holds the wheel on the axle.
That is the SDK's ambition: the smallest harness that makes complex agent orchestration feel like a cinch.

> 靈活簡便，以一馭萬。
> *Linh hoạt giản tiện, dĩ nhất ngự vạn.*
> *"Agile and simple — govern ten thousand things with one."*

## Install

```sh
pip install linch
```

For local development:

```sh
pip install -e '.[dev,mcp,anthropic]'
```

## Minimal agent

This snippet reads the API key from the environment. Do not hard-code keys in
source files or documentation.

```python
import asyncio
import os

from agent_kit import Agent
from agent_kit.sessions import InMemorySessionStore

agent = Agent(
    model="gpt-5",
    openai_api_key=os.environ.get("OPENAI_API_KEY"),
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},
)

async def main():
    session = await agent.session()
    async for event in session.run("What day is it?"):
        if event.type == "result":
            print(event.final_text)

asyncio.run(main())
```

For local runs, put `OPENAI_API_KEY=...` in `.env` or export it in your shell.
Never commit `.env`.

## Safe Local Demos

These examples run useful local code paths without requiring a live provider
call. They load `./.env` automatically when present, but never print secret
values.

```sh
python3 examples/tools/parallel_search_agent.py
python3 examples/tools/runtime_tools.py
python3 examples/context/rag_context_builder.py
python3 examples/memory/memory_agent.py
python3 examples/tools/tool_reliability_agent.py
python3 examples/memory/sqlite_memory_agent.py
```

The scripts skip live agent calls when `OPENAI_API_KEY` is missing. When a key
is present, only the examples with explicit live sections make provider calls.

```sh
# Filesystem offload demo — runs fully offline, no API key needed
python3 examples/tools/filesystem_offload.py
```

## Public API

- `linch`: `Agent`, `Session`, events, types, errors, `DetailedCompaction`, `RetryOptions`, `ToolTimeoutError`, `empty_tools`, `tools_from_defaults`
- `agent_kit.config`: `FeatureFlags`, `SystemPromptConfig`, `SystemPromptSection`
- `agent_kit.context`: `ContextBuilder`, `ContextBuildResult`, `ContextBudget`
- `agent_kit.skills`: built-in and project `SKILL.md` workflows, including `verify`
- `agent_kit.memory`: `MemoryStore`, `MemoryItem`, `MemoryContextBuilder`, `MemorySearchTool`, `MemoryUpsertTool`, reference stores
- `agent_kit.types`: `OutputSchema`, `ToolChoice`, `Message`, `ProviderRequest`
- `agent_kit.providers`: `OpenAIResponsesProvider`, `OpenAIChatCompletionsProvider`, `AnthropicProvider`
- `agent_kit.tools`: duck-typed tool protocol, `ResourceAccess`, `Citation`, `ToolResult`, `ToolRegistry`, built-in tools
- `agent_kit.sessions`: `InMemorySessionStore`, `SqliteSessionStore`
- `agent_kit.filesystem`: `FileBackend`, `StateFileBackend`, `DiskFileBackend`, `SqliteFileBackend`, `CompositeFileBackend`, `OffloadConfig`, `filesystem_tools`
- `agent_kit.permissions`: `PermissionEngine`, `ToolRule`, `PathRule`, `BashRule`
- `agent_kit.recipes`: scaffold factories (`rag_agent`, `sql_agent`, `doc_agent`, `build_agent`)

## Documentation

| File | What it covers |
|------|---------------|
| [`docs/usage.md`](docs/usage.md) | Getting started — install, config knobs, providers, tools, deps, structured output, RAG |
| [`docs/architecture.md`](docs/architecture.md) | Internal data flow, module contracts, design invariants |
| [`docs/contributing.md`](docs/contributing.md) | Dev setup, code rules, test conventions, PR checklist |
| [`examples/`](examples/) | Runnable scripts; local demos are safe without a key, live demos use `OPENAI_API_KEY` |

## Examples

Examples are organized by subsystem under `examples/`.

**`examples/core/`** — agent fundamentals

| File | What it shows |
|------|---------------|
| `core/minimal_agent.py` | Smallest possible agent |
| `core/custom_permissions.py` | Permission modes and rule types |
| `core/system_prompts.py` | append, replace, per-session, persona |
| `core/structured_output.py` | OutputSchema, final_tool_name, extraction |
| `core/event_streaming.py` | SSE, WebSocket, CLI progress |
| `core/multi_session.py` | One Agent, many users, persistent sessions |
| `core/loop_guard_agent.py` | LoopGuard — repeated tool call detection |
| `core/interactive_cli.py` | Interactive REPL |

**`examples/tools/`** — tool patterns and scheduler

| File | What it shows |
|------|---------------|
| `tools/custom_tools.py` | Tool patterns: read, write, exec, parallel, deps |
| `tools/parallel_search_agent.py` | Scheduler V2: parallel reads, resources, concurrency cap |
| `tools/runtime_tools.py` | Runtime `ToolRegistry.add/remove/replace/select` and schema export |
| `tools/tool_reliability_agent.py` | Timeout, per-tool opt-out, retry with `RetryOptions` |

**`examples/context/`** — RAG and context building

| File | What it shows |
|------|---------------|
| `context/context_injection.py` | ContextBuilder patterns: RAG per-turn, budget, tool selection |
| `context/rag_context_builder.py` | First-class ContextBuilder RAG with metadata and budget reporting |

**`examples/memory/`** — memory primitives

| File | What it shows |
|------|---------------|
| `memory/memory_agent.py` | Core memory primitives, search/upsert tools, ToolResult citations |
| `memory/sqlite_memory_agent.py` | SqliteMemoryStore — persistent memory, round-trip, upsert |

**`examples/observability/`** — observers and tracing

| File | What it shows |
|------|---------------|
| `observability/observability_agent.py` | LoggingObserver + optional OpenTelemetryObserver |
| `observability/custom_observer.py` | BaseObserver subclass: latency tracking, error counts |

**`examples/providers/`** — provider-specific features

| File | What it shows |
|------|---------------|
| `providers/anthropic_agent.py` | AnthropicProvider, thinking blocks, prompt caching |

**`examples/integrations/`** — subagents, skills, MCP

| File | What it shows |
|------|---------------|
| `integrations/subagent_coordinator.py` | Agent definitions, tool-filtered subagents, SubagentEvent |
| `integrations/multi_agent_isolation.py` | Context isolation: child work never enters parent context; sequential pipeline; parallel analysts; subagent + filesystem offload (*runs offline*) |

Use the safe local demos above for README-friendly behavior. Other examples may
make live provider calls when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is configured.

## Development

```sh
pytest
ruff check . && ruff format --check .
pyright
```

See [`docs/contributing.md`](docs/contributing.md) for the full contributor guide.
