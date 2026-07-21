# Linch Architecture

Professional reference for the V2 harness. Covers every subsystem, its contract, the complete data flow, and the invariants that must not break.

---

This guide is split by topic so you can jump to a subsystem instead of scrolling one long file. For *how to use* these features, see the [usage guide](../usage/README.md).

## Sections

| Section | What it covers |
|---|---|
| 1. [System Overview](./overview.md) | System overview, layering, and the top-level data-flow diagram |
| 2. [Turn Lifecycle](./turn-lifecycle.md) | What happens on each turn: request assembly → provider stream → tools → repeat |
| 3. [Subsystems](./subsystems.md) | The subsystems in depth: loop, providers, tools, permissions, memory, filesystem, more |
| 4. [Event Taxonomy](./events.md) | The event taxonomy — every event type the loop emits |
| 5. [Key Data Types](./data-types.md) | Key data types: messages, blocks, requests, results, usage |
| 6. [Module Inventory](./module-inventory.md) | Module-by-module inventory of the source tree |
| 7. [Provider Contract](./provider-contract.md) | The provider contract: context_window / stream / capabilities |
| 8. [Tool Protocol](./tool-protocol.md) | The duck-typed tool protocol and tool result shape |
| 9. [System Prompt Layers](./system-prompt.md) | How the system prompt is assembled from layered sections |
| 10. [Structured Output Paths](./structured-output.md) | Structured-output paths: final-text vs final-tool capture, schema repair |
| 11. [Compaction](./compaction.md) | Compaction: when and how old context is summarized |
| 12. [Skills and Subagents](./skills-subagents.md) | Skills (slash commands) and subagents (roles, workers, fork/continue) |
| 13. [Key Invariants](./invariants.md) | Key invariants the whole system upholds |
