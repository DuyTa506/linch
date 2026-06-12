# Using Linch in Your Project

> This guide has moved. It is now split by topic under
> [**`docs/usage/`**](./usage/README.md) so you can jump straight to what you
> need instead of scrolling one long file.

Start at the [usage guide index](./usage/README.md), or go directly to a topic:

| Page | What it covers |
|---|---|
| [Agent & session](./usage/agent.md) | Session stores, feature flags, system prompt, compaction, run budgets |
| [Providers](./usage/providers.md) | Choosing a provider, model catalog, capabilities, thinking events |
| [Events](./usage/events.md) | Event stream, cost fields, run reports, long-run eval scorers |
| [Tools](./usage/tools.md) | `@tool`, `FunctionTool`, class tools, scheduler, timeouts/retry, Bash backend, deps, permissions |
| [Structured output](./usage/structured-output.md) | `OutputSchema`, final-tool capture, schema repair |
| [Hooks](./usage/hooks.md) | The canonical extension mechanism: chokepoints, `HookResult`, built-in adapters |
| [Context & memory](./usage/context-and-memory.md) | Per-turn RAG context building, memory primitives, tiered memory |
| [Virtual filesystem](./usage/filesystem.md) | Large-result offloading and the virtual filesystem tools |
| [Workflows](./usage/workflows.md) | Deterministic fleet loops (`run_workflow`), journaling, resume |
| [Deep agent](./usage/deep-agent.md) | `create_deep_agent`, background workers, fork/continue, coordinator mode |
| [Skills](./usage/skills.md) | Slash-command prompt workflows, the built-in `verify` skill |
| [Examples](./usage/examples.md) | Index of runnable examples under `examples/` |

For the design and data flow behind these features, see
[`architecture.md`](./architecture.md).
