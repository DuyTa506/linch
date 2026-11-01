# Linch 3.0 Architecture

Reference for the headless Linch agent kernel. It describes the current
subsystem contracts, data flow, durability boundaries, and invariants.

---

This guide is split by topic so you can jump to a subsystem instead of scrolling one long file. For *how to use* these features, see the [usage guide](../usage/README.md).

The SDK/application boundary and the Linch 3.0 compatibility changes are
summarized in the [migration guide](../migration-3.0.md).

## Sections

| Section | What it covers |
|---|---|
| 1. [System Overview](./overview.md) | Headless kernel, host boundary, deployment topology, and data flow |
| 2. [Turn Lifecycle](./turn-lifecycle.md) | What happens on each turn: request assembly → provider stream → tools → repeat |
| 3. [Subsystems](./subsystems.md) | The subsystems in depth: loop, providers, tools, permissions, context/RAG, filesystem, and more |
| 4. [Event Taxonomy](./events.md) | The event taxonomy — every event type the loop emits |
| 5. [Key Data Types](./data-types.md) | Key data types: messages, blocks, requests, results, usage |
| 6. [Module Inventory](./module-inventory.md) | Module-by-module inventory of the source tree |
| 7. [Provider Contract](./provider-contract.md) | The provider contract: context_window / stream / capabilities |
| 8. [Tool Protocol](./tool-protocol.md) | The duck-typed tool protocol and tool result shape |
| 9. [System Prompt Layers](./system-prompt.md) | How the system prompt is assembled from layered sections |
| 10. [Structured Output Paths](./structured-output.md) | Structured-output paths: final-text vs final-tool capture, schema repair |
| 11. [Compaction](./compaction.md) | Compaction: when and how old context is summarized |
| 12. [Skills and Subagents](./skills-subagents.md) | Skills (slash commands) and subagents (roles, workers, fork/continue) |
| 13. [Kernel](./kernel.md) | The IoC/effect kernel: `Context` scopes, reversible `Disposable` effects, the event bus (emit/serial/waterfall), the open tool pipeline, and capability seams |
| 14. [Development Principles](./development_principles.md) | Scope, extension, durability, compatibility, async, and security rules for contributors |
| 15. [Key Invariants](./invariants.md) | Key invariants the whole system upholds |
| 16. [Session-log read performance](./session-log-performance.md) | D6 benchmark, compatibility analysis, and projection-reader inventory |

## Linch 3.0 feature index

The 3.0 surface is deliberately split across architecture and usage pages.
Use this map when reviewing a feature boundary or its migration impact.

| Feature | Contract and usage |
|---|---|
| Plugin mounting and `agent.use` | [Kernel plugins](./kernel.md#plugins) and [profile plugin rows](../usage/profiles.md#layers-and-inspection) |
| Versioned domain profiles | [Domain profiles](../usage/profiles.md) and [profile scopes](./kernel.md#domain-profiles) |
| Deferred tool/skill search | [Deferred tool search](./tool-protocol.md#deferred-tool-search) and [large catalogs](../usage/tools.md#tool-search-for-large-catalogs) |
| Typed lifecycle catalog and trace IDs | [Generated lifecycle catalog](./events.md#typed-lifecycle-catalog) and [event trace IDs](../usage/events.md#opentelemetry-traces-and-metrics) |
| Per-run principals and RBAC rules | [Permission evaluation](./subsystems.md#37-permission-evaluation-and-principal) and [role rules](../usage/tools.md#per-run-principals-and-role-rules) |
| RAG retrieval and citation grounding | [Context/RAG usage](../usage/context-and-memory.md#sourced-retrieval-and-grounding) and [RAG subsystem](./subsystems.md#38-context-file-memory-and-rag) |
| Postgres runs, leases, and fencing | [Run stores](../usage/run-stores.md) and [durable-run subsystem](./subsystems.md#33-durable-runs-leases-and-checkpoints) |
| PgQueuer worker fleet and schedules | [Production scale-out](../usage/production.md#scale-out) and [worker recovery](./subsystems.md#34-worker-dispatch-and-pgqueuer-recovery) |
| File-backed memory | [File memory usage](../usage/context-and-memory.md#file-memory) and [RAG/files subsystem](./subsystems.md#38-context-file-memory-and-rag) |
| OpenTelemetry source instrumentation | [Tracing and metrics](../usage/events.md#opentelemetry-traces-and-metrics) and [OTel architecture](./subsystems.md#310-opentelemetry) |
| Explicit `linch.ext.*` extensions | [Extension boundary](./overview.md#extensions) and [extension subsystem](./subsystems.md#311-extensions-and-host-owned-boundaries) |
| Reliability and turn-stopping | [Turn lifecycle](./turn-lifecycle.md#typed-lifecycle-points) and [reliability usage](../usage/agent.md#tool-and-response-reliability) |
| Unified execution worlds | [Execution usage](../usage/extending.md#executionbackend--one-shell--filesystem-world) and [tool protocol](./tool-protocol.md#toolregistry) |
| Append-only session log and projections | [Session invariants](./invariants.md) and [session-log performance](./session-log-performance.md) |
| Model-call accounting and compaction | [Accounting events](../usage/events.md#model-call-accounting) and [compaction](./compaction.md) |
| Canonical tool output and durable delivery | [Tool protocol](./tool-protocol.md) and [agent/session usage](../usage/agent.md) |
| Wire generation 3 and Python 3.11+ | [Migration guide](../migration-3.0.md) and [versioning contract](../versioning.md) |
