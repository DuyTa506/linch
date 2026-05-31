# agent_kit

AgentKit is a Python SDK for building agent loops you embed in your own application. It is async-first, event-driven, and provider-agnostic — designed as a **harnessing framework** you build domain workflows on top of.

Core capabilities: streaming events, per-turn context injection (RAG), structured output, typed tool protocol, permission engine, MCP/skills/subagents, pluggable providers.

## Install

```sh
pip install agent-kit
```

For local development:

```sh
pip install -e '.[dev,mcp,anthropic]'
```

## Minimal agent

```python
import asyncio
from agent_kit import Agent
from agent_kit.sessions import InMemorySessionStore

agent = Agent(
    model="gpt-5",
    openai_api_key="sk-...",
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

## Public API

- `agent_kit`: `Agent`, `Session`, events, types, errors, `empty_tools`, `tools_from_defaults`
- `agent_kit.config`: `FeatureFlags`, `SystemPromptConfig`
- `agent_kit.context_hooks`: `ContextInjector`, `TurnContext`, `prune_tagged`
- `agent_kit.types`: `OutputSchema`, `ToolChoice`, `Message`, `ProviderRequest`
- `agent_kit.providers`: `OpenAIResponsesProvider`, `OpenAIChatCompletionsProvider`, `AnthropicProvider`
- `agent_kit.tools`: duck-typed tool protocol, `ToolRegistry`, built-in tools
- `agent_kit.sessions`: `InMemorySessionStore`, `SqliteSessionStore`
- `agent_kit.permissions`: `PermissionEngine`, `ToolRule`, `PathRule`, `BashRule`
- `agent_kit.recipes`: scaffold factories (`rag_agent`, `sql_agent`, `doc_agent`, `build_agent`)

## Documentation

| File | What it covers |
|------|---------------|
| [`docs/usage.md`](docs/usage.md) | Getting started — install, config knobs, providers, tools, deps, structured output, RAG |
| [`docs/architecture.md`](docs/architecture.md) | Internal data flow, module contracts, design invariants |
| [`docs/contributing.md`](docs/contributing.md) | Dev setup, code rules, test conventions, PR checklist |
| [`examples/`](examples/) | Runnable scripts (set `OPENAI_API_KEY`) |

## Examples

| File | What it shows |
|------|---------------|
| `examples/minimal_agent.py` | Smallest possible agent |
| `examples/01_custom_tools.py` | Tool patterns: read, write, exec, parallel, deps |
| `examples/02_custom_permissions.py` | Permission modes and rule types |
| `examples/03_system_prompts.py` | append, replace, per-session, persona |
| `examples/04_structured_output.py` | OutputSchema, final_tool_name, extraction |
| `examples/05_context_injection.py` | RAG per-turn, sliding-window, vector search |
| `examples/06_multi_session.py` | One Agent, many users, persistent sessions |
| `examples/07_event_streaming.py` | SSE, WebSocket, CLI progress |

## Development

```sh
pytest
ruff check . && ruff format --check .
pyright
```

See [`docs/contributing.md`](docs/contributing.md) for the full contributor guide.
