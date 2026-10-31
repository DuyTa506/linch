# Using Linch in Your Project

> This guide has moved. It is now split by topic under
> [**`docs/usage/`**](./usage/README.md) so you can jump straight to what you
> need instead of scrolling one long file.

Start with the [quickstart](./usage/quickstart.md) if you are new to Linch, then
use the [usage guide index](./usage/README.md) or jump directly to a topic:

| Page | What it covers |
|---|---|
| [Quickstart](./usage/quickstart.md) | Install, offline smoke test, live provider, first tool, persistence |
| [Agent & session](./usage/agent.md) | Session stores, feature flags, system prompt, compaction, run budgets |
| [Plugins](./architecture/kernel.md#plugins) | Mount with `agent.use`, dependency activation, reversible disposal |
| [Domain profiles](./usage/profiles.md) | Layered domain configuration, per-run selection, and version-pinned resume |
| [Tool search](./usage/tools.md#tool-search-for-large-catalogs) | Deferred tool schemas and skill listings for large catalogs |
| [Durable runs](./usage/run-stores.md) | Postgres checkpoints, leases, and replay by sequence |
| [Worker fleet](./usage/production.md#scale-out) | PgQueuer dispatch, recovery, and graceful drain |
| [Lifecycle and trace IDs](./architecture/events.md#typed-lifecycle-catalog) | Typed dispatch catalog and host trace correlation |
| [Providers](./usage/providers.md) | Choosing a provider, model catalog, capabilities, thinking events |
| [Events](./usage/events.md) | Event stream, cost fields, run reports, long-run eval scorers |
| [Tools](./usage/tools.md) | `@tool`, `FunctionTool`, class tools, scheduler, timeouts/retry, Bash backend, deps, principals and role permissions |
| [Structured output](./usage/structured-output.md) | `OutputSchema`, final-tool capture, schema repair |
| [Hooks](./usage/hooks.md) | The canonical extension mechanism: chokepoints, `HookResult`, built-in adapters |
| [Context & memory](./usage/context-and-memory.md) | RAG connectors and grounding, per-turn context, scoped file memory, and the optional mem0 recipe |
| [Virtual filesystem](./usage/filesystem.md) | Large-result offloading and the virtual filesystem tools |
| [Workflows](./usage/workflows.md) | Deterministic fleet loops (`run_workflow`), journaling, resume |
| [Deep agent](./usage/deep-agent.md) | `create_deep_agent`, background workers, fork/continue, coordinator mode |
| [Skills](./usage/skills.md) | Slash-command prompt workflows, the built-in `verify` skill |
| [Examples](./usage/examples.md) | Index of runnable examples under `examples/` |
| [Explicit extensions](./architecture/overview.md#extensions) | The `linch.ext` namespace for workflows, evals, deep agents, reports, loop runners, and scaffolding |

For the design and data flow behind these features, see
[`architecture.md`](./architecture.md).
