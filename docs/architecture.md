# Linch Architecture

Use this index to find Linch subsystem contracts, data flow, and invariants.

Start at the [architecture index](./architecture/README.md), or go directly to a section:

| Section | What it covers |
|---|---|
| 1. [System Overview](./architecture/overview.md) | System overview, layering, and the top-level data-flow diagram |
| 2. [Turn Lifecycle](./architecture/turn-lifecycle.md) | What happens on each turn: request assembly → provider stream → tools → repeat |
| 3. [Subsystems](./architecture/subsystems.md) | The subsystems in depth: loop, providers, tools, permissions, memory, filesystem, more |
| 4. [Event Taxonomy](./architecture/events.md) | Stream events, persistence boundaries, and lifecycle dispatch points |
| 5. [Key Data Types](./architecture/data-types.md) | Key data types: messages, blocks, requests, results, usage |
| 6. [Module Inventory](./architecture/module-inventory.md) | Module-by-module inventory of the source tree |
| 7. [Provider Contract](./architecture/provider-contract.md) | The provider contract: context_window / stream / capabilities |
| 8. [Tool Protocol](./architecture/tool-protocol.md) | The duck-typed tool protocol and tool result shape |
| 9. [System Prompt Layers](./architecture/system-prompt.md) | How the system prompt is assembled from layered sections |
| 10. [Structured Output Paths](./architecture/structured-output.md) | Structured-output paths: final-text vs final-tool capture, schema repair |
| 11. [Compaction](./architecture/compaction.md) | Compaction: when and how old context is summarized |
| 12. [Skills and Subagents](./architecture/skills-subagents.md) | Skills (slash commands) and subagents (roles, workers, fork/continue) |
| 13. [Key Invariants](./architecture/invariants.md) | Key invariants the whole system upholds |
| [Plugins and profiles](./architecture/kernel.md#plugins) | `agent.use`, dependency activation, scoped effects, and per-run configuration |
| [Tool search](./architecture/tool-protocol.md#deferred-tool-search) | Deferred schemas and persistent discovery state |
| [Lifecycle catalog and trace IDs](./architecture/events.md#typed-lifecycle-catalog) | Typed dispatch modes, producers/consumers, and trace correlation |
| [Principal/RBAC](./usage/tools.md#per-run-principals-and-role-rules) | Per-run identity, role-conditioned permissions, and resume checks |
| [RAG and grounding](./usage/context-and-memory.md#ragconnector-and-register_grounding_check) | Sourced retrieval and citation checks on the finalization seam |
| [Worker fleet](./usage/production.md#scale-out) | PgQueuer dispatch and leased durable execution |
| [Explicit extensions](./architecture/overview.md#extensions) | The `linch.ext` boundary |

For *how to use* these features, see the [usage guide](./usage/README.md).
