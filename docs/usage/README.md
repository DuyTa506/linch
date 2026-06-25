# Using Linch

This is the practical guide to building with Linch — installing it, wiring an
agent, and using each subsystem. It is split by topic so you can jump straight
to what you need instead of scrolling one long file.

For the *why* behind the design (data flow, the turn pipeline, module
boundaries) see [`../architecture.md`](../architecture.md).

---

## Install

```bash
# From the repo (development)
pip install -e /path/to/linch

# With common optional extras
pip install -e "/path/to/linch[mcp,anthropic,gemini,postgres]"
```

Once published, `pip install linch` will work directly. Extras are opt-in:
`anthropic`, `gemini`, `postgres`, `mcp`, and `otel` each pull in only the
dependencies that feature needs.

---

## Minimum working agent

```python
import asyncio
from linch import Agent
from linch.sessions import InMemorySessionStore

agent = Agent(
    model="gpt-5",                              # or any supported model
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
| [Agent & session](./agent.md) | Session stores, feature flags, system prompt, compaction, run budgets |
| [Providers](./providers.md) | Choosing a provider, model catalog, capabilities, thinking events |
| [Events](./events.md) | Event stream, cost fields, run reports, long-run eval scorers |
| [Evals and benchmarks](./evals.md) | `EvalSuite`, `run_eval_benchmark`, and `scripts/eval_benchmark.py` for CI/provider comparison |
| [Tools](./tools.md) | `@tool`, `FunctionTool`, class tools, scheduler, timeouts/retry, Bash backend, deps, permissions |
| [Structured output](./structured-output.md) | `OutputSchema`, final-tool capture, schema repair |
| [Hooks](./hooks.md) | The canonical extension mechanism: chokepoints, `HookResult`, built-in adapters |
| [Tool cache](./tool-cache.md) | Opt-in per-run memoization of read-scope tool calls (`tool_cache=`) |
| [Context & memory](./context-and-memory.md) | Per-turn RAG context building, memory primitives, tiered memory |
| [Vector memory adapters](./vector-memory-adapters.md) | FAISS, pgvector, and Qdrant recipes using the existing `MemoryStore` seam |
| [Virtual filesystem](./filesystem.md) | Large-result offloading and the `ls`/`read_file`/`write_file`/`edit_file` tools |
| [Workflows](./workflows.md) | Deterministic fleet loops (`run_workflow`), journaling, resume |
| [Outer loop runner](./loop-runner.md) | `LoopSpec` + `LoopRunner.run_once()` for cron, CI, webhooks, and manual recurring work |
| [Deep agent](./deep-agent.md) | `create_deep_agent`, background workers, fork/continue, coordinator mode |
| [Coordination](./coordination.md) | Advance the loop from a clock or a peer: scheduling (cron/interval) and multi-agent teams (mailbox + correlator) |
| [Skills](./skills.md) | Slash-command prompt workflows, the built-in `verify` skill |
| [Extending](./extending.md) | Implement your own seam: `IsolationBackend`, `Mailbox`, `ScheduleStore`, prompt assembly, memory lifecycle |
| [Examples](./examples.md) | Index of runnable examples under `examples/` |

---

## See `examples/` for runnable code

Many subsystems ship a runnable demo — several run fully offline without an API
key. The full index is in [examples.md](./examples.md).
