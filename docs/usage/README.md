# Using Linch

This is the practical guide to building with Linch — installing it, wiring an
agent, and using each subsystem. If you are new to the SDK, start with
[Quickstart](./quickstart.md); then come back here for the subsystem map.

For the *why* behind the design (data flow, the turn pipeline, module
boundaries) see [`../architecture.md`](../architecture.md).

If you are upgrading to Linch 3.0, read the
[Linch 3.0 migration guide](../migration-3.0.md) first. Linch is an SDK: a
coding workspace and its trust policy are explicit choices at the call site.

---

## Install

```bash
# From the repo (development)
pip install -e /path/to/linch

# With common optional extras
pip install -e "/path/to/linch[mcp,anthropic,gemini,postgres]"
```

Once published, `pip install linch` will work directly. Extras are opt-in:
`anthropic`, `gemini`, `postgres`, and `mcp` each pull in only the dependencies
that feature needs. OpenTelemetry's API is part of the core install; hosts add
the SDK and an exporter when they want to collect traces or metrics.

---

## Minimum working agent

```python
import asyncio
from linch import Agent
from linch.sessions import InMemorySessionStore

agent = Agent(
    model="gpt-5",                              # or any supported model
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},     # allow tools without prompting
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
Agent ──── long-lived config (model, tools, permissions, system prompt, hooks, deps)
  └── Session ── conversation state (messages, run_deps, live workers)
        └── session.run(prompt) ── AsyncIterator[Event]
```

- **`Agent`** is created once per model/configuration and reused across many
  conversations. It is immutable config plus a few subsystem handles (provider,
  tool registry, permission engine, stores).
- **`Session`** is one conversation thread. A user in a web app gets their own
  session but shares the same `Agent`. State that belongs to a conversation
  (history, live background workers, per-run deps) lives here.
- **`session.run(prompt)`** returns an async iterator of typed
  [events](./events.md) — stream them to your UI as they arrive. The last event
  is always a `ResultEvent`.

One `Agent`, many `Session`s is the canonical web-app shape — see
[`examples/core/multi_session.py`](../../examples/core/multi_session.py).

---

## Guide map

| Page | What it covers |
|---|---|
| [Quickstart](./quickstart.md) | New-developer path: install, offline smoke test, live provider, first tool, persistence |
| [Scaffolding CLI](./cli.md) | `linch new` / `linch add tool` — generate a runnable, offline-testable project you own |
| [Production wiring](./production.md) | Minimal checklist to embed Linch in a service: persistence, streaming, HITL, resume, shutdown, blast radius |
| [Run stores](./run-stores.md) | Durable checkpoints, Postgres, optional leases and fencing, reconnect by event sequence |
| [Agent & session](./agent.md) | Session stores, feature flags, system prompt, compaction, run budgets, truncation recovery |
| [Domain profiles](./profiles.md) | Versioned per-run roles, plugin configuration, tool restrictions, limits, layering, and resume pinning |
| [Providers](./providers.md) | Choosing a provider, model catalog, capabilities, thinking events |
| [Events](./events.md) | Event stream, cost fields, run reports, long-run eval scorers |
| [Evals and benchmarks](./evals.md) | `EvalSuite`, `run_eval_benchmark`, and `scripts/eval_benchmark.py` for CI/provider comparison |
| [Tools](./tools.md) | `@tool`, `FunctionTool`, class tools, scheduler, timeouts/retry, Bash backend, deps, permissions, per-run principals and roles |
| [Structured output](./structured-output.md) | `OutputSchema`, final-tool capture, schema repair |
| [Hooks](./hooks.md) | The canonical extension mechanism: chokepoints, `HookResult`, built-in adapters |
| [Tool cache](./tool-cache.md) | Opt-in per-run memoization of read-scope tool calls (`tool_cache=`) |
| [Context & memory](./context-and-memory.md) | `RagConnector` retrieval and grounding, per-turn context, scoped file memory, and the optional mem0 recipe |
| [Virtual filesystem](./filesystem.md) | Large-result offloading and the `ls`/`read_file`/`write_file`/`edit_file` tools |
| [Workflows](./workflows.md) | Deterministic fleet loops (`run_workflow`), journaling, resume |
| [Outer loop runner](./loop-runner.md) | `LoopSpec` + `LoopRunner.run_once()` for cron, CI, webhooks, and manual recurring work |
| [Deep agent](./deep-agent.md) | `create_deep_agent`, background workers, fork/continue, coordinator mode |
| [Coordination](./coordination.md) | Multi-agent teams with mailbox messaging and correlator handshakes |
| [Skills](./skills.md) | Slash-command prompt workflows, the built-in `verify` skill |
| [Extending](./extending.md) | Implement your own seam: `IsolationBackend`, `Mailbox`, prompt assembly, and context builders |
| [Examples](./examples.md) | Index of runnable examples under `examples/` |

---

## See `examples/` for runnable code

Many subsystems ship a runnable demo — several run fully offline without an API
key. The full index is in [examples.md](./examples.md).
