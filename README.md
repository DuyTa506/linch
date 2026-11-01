# linch

Linch 3.0 is a headless Python agent kernel. Applications embed its async agent
loop, provide tools and permissions, and consume streamed events. The host owns
the HTTP API and deployment.

Python 3.11+ is required. The current package version is `3.0.0a1`; 3.0 is under
development and breaks compatibility with 2.x. See the
[3.0 migration guide](docs/migration-3.0.md).

## Features

| Area | Implementation |
|---|---|
| Runtime | `Agent`, `Session`, typed event streams, tool scheduling, context compaction, and run budgets |
| Plugins | [`agent.use(plugin, config)`](docs/architecture/kernel.md#plugins) mounts scoped plugins with dependency activation and reversible disposal |
| Profiles | [Versioned domain profiles](docs/usage/profiles.md) compose configuration and pin each run's profile for resume |
| Tool search | [Deferred tool and skill discovery](docs/usage/tools.md#tool-search-for-large-catalogs) loads schemas when needed above configurable thresholds |
| Durable runs | Postgres session/run stores, leases with epoch fencing, checkpoints before guarded side effects, and event replay by sequence |
| Worker fleet | Optional `linch.worker`: PgQueuer dispatch, lease renewal, recovery after worker death or transient database outages, and graceful drain |
| Reliability | Default bounded response correction, optional tool prerequisites and completion gates, and a serial turn-stopping seam |
| Permissions | Per-run `Principal`, tool/path/shell rules with role conditions, and durable approval decisions |
| Retrieval and memory | `RagConnector`, citation grounding checks, scoped file memory, and virtual filesystem backends |
| Providers | OpenAI Responses/Chat Completions, Anthropic, Gemini/Vertex AI through `google-genai`, DeepSeek, llama.cpp, and compatible endpoints |
| Extensions | Explicit `linch.ext` packages for workflows, evals, deep agents, reports, loop runners, and scaffolding; hooks, MCP, skills, and subagents |
| Telemetry | Direct OpenTelemetry traces and metrics; the host configures the SDK and exporter |
| Lifecycle | [Typed lifecycle catalog and trace IDs](docs/architecture/events.md#typed-lifecycle-catalog) connect hooks, kernel seams, events, and host logs |

Postgres is the durable backend. InMemory stores are for tests and development.
Read-scoped tools can run in parallel. An input-aware `parallel` predicate also
allows read-only subagent calls; write/exec tools with boolean flags stay serial.

## Install

From a checkout:

```sh
pip install -e .
```

Install the extras your application uses:

```sh
pip install -e '.[postgres,worker,anthropic,gemini,mcp]'
```

OpenTelemetry's API is included in core. Install `opentelemetry-sdk` and an
exporter in the host to collect telemetry.

## Quickstart

This example runs without credentials or network access. `ScriptedProvider` is
an eval extension that supplies a fixed response.

```python
import asyncio

from linch import Agent, InMemorySessionStore
from linch.ext.evals import ScriptedProvider, TextTurn


async def main() -> None:
    """Run one offline conversation and print its result."""
    agent = Agent(
        model="demo-model",
        provider=ScriptedProvider([TextTurn(text="Hello from Linch.")]),
        session_store=InMemorySessionStore(),
    )
    try:
        session = await agent.session()
        async for event in session.run("Say hello"):
            if event.type == "result":
                print(event.final_text)
    finally:
        await agent.close()


asyncio.run(main())
```

Expected output: `Hello from Linch.` For a live provider, tools, and durable
stores, follow the [quickstart guide](docs/usage/quickstart.md). Production
hosts configure both session and run stores explicitly; the
[production guide](docs/usage/production.md) covers workers, streaming, resume,
and shutdown.

## Documentation

- [Usage index](docs/usage/README.md): setup and examples for each feature.
- [Architecture index](docs/architecture/README.md): module boundaries, contracts, and invariants.
- [Migration from 2.x](docs/migration-3.0.md): removed APIs and replacement imports.
- [Versioning](docs/versioning.md): public API, prereleases, and persisted formats.
- [Examples](examples/): runnable examples grouped by subsystem.
- [Contributing](docs/contributing.md): development and review rules.

Kernel imports come from `linch.__all__`. Import extensions explicitly, for
example `from linch.ext.workflow import run_workflow`; they are absent from the
kernel's top-level exports.

## Development

```sh
pip install -e '.[dev,postgres,worker,mcp,anthropic,gemini]'
python -m pytest -q
ruff check . && ruff format --check .
pyright
```
