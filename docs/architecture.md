# Linch Architecture

> This guide has moved. It is now split by topic under [**`docs/architecture/`**](./architecture/README.md) so you can jump straight to a subsystem instead of scrolling one long file.

Start at the [architecture index](./architecture/README.md), or go directly to a section:

| Section | What it covers |
|---|---|
| 1. [System Overview](./architecture/overview.md) | System overview, layering, and the top-level data-flow diagram |
| 2. [Turn Lifecycle](./architecture/turn-lifecycle.md) | What happens on each turn: request assembly → provider stream → tools → repeat |
| 3. [Subsystems](./architecture/subsystems.md) | The subsystems in depth: loop, providers, tools, permissions, memory, filesystem, more |
| 4. [Event Taxonomy](./architecture/events.md) | The event taxonomy — every event type the loop emits |
| 5. [Key Data Types](./architecture/data-types.md) | Key data types: messages, blocks, requests, results, usage |
| 6. [Module Inventory](./architecture/module-inventory.md) | Module-by-module inventory of the source tree |
| 7. [Provider Contract](./architecture/provider-contract.md) | The provider contract: context_window / stream / capabilities |
| 8. [Tool Protocol](./architecture/tool-protocol.md) | The duck-typed tool protocol and tool result shape |
| 9. [System Prompt Layers](./architecture/system-prompt.md) | How the system prompt is assembled from layered sections |
| 10. [Structured Output Paths](./architecture/structured-output.md) | Structured-output paths: final-text vs final-tool capture, schema repair |
| 11. [Compaction](./architecture/compaction.md) | Compaction: when and how old context is summarized |
| 12. [Skills and Subagents](./architecture/skills-subagents.md) | Skills (slash commands) and subagents (roles, workers, fork/continue) |
| 13. [Key Invariants](./architecture/invariants.md) | Key invariants the whole system upholds |

For *how to use* these features, see the [usage guide](./usage/README.md).
