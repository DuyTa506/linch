# Linch SDK Roadmap

This is the active roadmap for Linch as a **pure-mechanism, embeddable agent
runtime SDK**. It records the next work, its evidence, the compatibility rules
that constrain it, and the measurements that define completion.

Completed roadmap slices do not remain here as active work. When a phase ships,
move its durable contracts into architecture or usage documentation and replace
it with the next evidence-backed priority.

**Status (July 2026):** the audit-driven hardening program — measurement
guardrails, runtime reliability, lifecycle and scalability, provider parity, and
durable context — has shipped. Its durable contracts now live in the
architecture and usage docs (see [Recently shipped](#recently-shipped) below).
There is no active phase queued; ranked prospects live in
[Next candidates](#next-candidates) and enter active work through the
[acceptance gate](#roadmap-item-acceptance-gate) when evidence justifies it.

---

## North Star

Linch ships the **harness**, not the agent. It provides mechanisms, protocols,
seams, and primitives: loop control, tools, permissions, memory, context,
events, durability, scheduling, subagents, hooks, evaluation, and observability.

A coding agent is one application an embedder builds on Linch. Support,
research, data, operations, and product-specific agents must remain equally
first-class.

The discriminating rule for every roadmap item is:

> Is it a mechanism many embedded agents can use, or behavior and policy for one
> kind of agent? Mechanism goes in Linch. Policy stays with the embedder.

When a useful capability is domain-flavored, Linch exposes the generic seam and
the embedder supplies the domain implementation. For example,
`ExecutionBackend` belongs in Linch; a git-worktree coding runner does not.

### Hard constraints

- New capabilities are additive and opt-in unless they are domain-neutral
  correctness, security, lifecycle, or performance fixes.
- Defaults preserve event order, provider-order tool results, checkpoint
  semantics, and observable loop behavior.
- The public surface remains exactly `linch.__all__`; changes are deliberate and
  follow `docs/versioning.md`.
- Duck-typed protocols do not gain required methods in a minor release. New
  protocol capabilities are detected with `getattr` or `hasattr` and have a
  compatibility fallback.
- Persisted wire formats remain forward-tolerant. Breaking shapes require an
  explicit schema-version migration.
- Python 3.10 remains supported. Do not use newer runtime primitives without a
  compatible implementation or backport.
- The core dependency graph stays small. Heavy integrations belong in extras,
  examples, or separate packages.
- Linch remains embeddable inside a host service. It does not become a daemon,
  dashboard, marketplace, or hosted agent product.

---

## Current Runtime Baseline

Linch already contains the main runtime substrate:

- Event-driven `Agent` / `Session` execution with typed streaming events.
- OpenAI, Anthropic, Gemini, llama.cpp, vLLM, SGLang, and OpenAI-compatible
  providers.
- Tools, MCP wrapping, permissions, retries, timeouts, resource-aware
  scheduling, isolation, and background execution.
- Context builders, compaction, memory stores, virtual filesystems, and result
  offload.
- Durable sessions, run checkpoints, workflow journals, run reports, and resume.
- Subagents, retained workers, workflows, mailbox coordination, and scheduling.
- Hooks, OpenTelemetry integration, deterministic evals, and extension contract
  helpers.

The baseline is broad enough. The roadmap prioritizes runtime reliability,
lifecycle ownership, measured scalability, provider parity, and internal
simplicity rather than adding new orchestration layers.

### Measurement discipline

Performance work is gated by the offline benchmark suite
(`scripts/benchmark_runtime.py`): two warmups and at least ten measured samples
per case, reporting median and p95 latency, event-loop heartbeat lag, and
cold-import RSS alongside a correctness oracle. Absolute timing thresholds live
in a dedicated fixed-runner or nightly job; normal CI asserts correctness,
bounded work, event ordering, and algorithmic complexity without fragile
wall-clock limits. The July 2026 audit baselines (blocking-bridge tail latency,
quadratic context trimming, memory-search heartbeat stalls, eager cold import,
full-batch scheduler barrier, unreleased sessions, synchronous provider I/O)
were regressions to fix, not universal hardware claims; the outcomes below
resolved them and the suite guards against their return.

---

## Recently shipped

The hardening program landed as additive, opt-in mechanisms. Each item's durable
contract now lives in the docs linked below; this table is a pointer, not active
work.

| Area | Outcome | Contract lives in |
|---|---|---|
| Measurement guardrails | Offline benchmark suite with warmups, samples, p50/p95, heartbeat lag, cold-import RSS, and correctness oracles | `scripts/benchmark_runtime.py` |
| Blocking-bridge latency | `run_blocking` is callback-driven with a dormant timer fallback (no polling tail) | [architecture/invariants.md](./architecture/invariants.md) |
| Durable write amplification | Event log is the tool-execution recovery source; checkpoints saved once per batch, not per tool start/end | [architecture/turn-lifecycle.md](./architecture/turn-lifecycle.md) |
| Ordered teardown | `Agent.close()`, session release, abort, and scheduler cancellation drain owned work before closing resources | [usage/agent.md](./usage/agent.md) |
| Provider warm-up | Optional duck-typed `provider.prepare()` coalesced once before the first run; llama.cpp context discovery leaves the event loop | [architecture/provider-contract.md](./architecture/provider-contract.md) |
| Session lifecycle | `agent.release_session(...)`, `session.aclose(force=...)`, and `Session` as an async context manager | [usage/agent.md](./usage/agent.md#releasing-a-single-session) |
| Linear context trimming | Trimming is linear over message count; retained messages, order, estimator calls, and `ContextBudget` fields unchanged | [architecture/compaction.md](./architecture/compaction.md) |
| Scalable reference memory | Namespace-partitioned in-memory store, chunked cooperative yields, bounded top-k, off-loop SQLite/Postgres scoring | [usage/context-and-memory.md](./usage/context-and-memory.md) |
| Scheduler parallelism | Provider-order results without the full-batch barrier; opt-in maximal-compatible batching, greedy default unchanged | [architecture/tool-protocol.md](./architecture/tool-protocol.md) |
| Provider conformance | `assert_provider_contract` (`linch.testing`), transport `aclose()` on Anthropic/OpenAI Responses, Gemini tool-choice mapping | [architecture/provider-contract.md](./architecture/provider-contract.md) |
| Lazy public exports | PEP 562 lazy export map keeps `import linch` off MCP/Uvicorn/unused provider SDKs; `linch.__all__` and star imports unchanged | `docs/versioning.md` |
| Durable compacted views | Optional store-detected `save_provider_snapshot`/`load_provider_snapshot`; reload restores the view and appends newer messages | [architecture/compaction.md](./architecture/compaction.md#durable-compacted-views-opt-in-store-detected) |
| Idempotent integration seam | Stable `ToolContext.idempotency_key` (run id + tool-use id) for at-least-once reconciliation | [architecture/tool-protocol.md](./architecture/tool-protocol.md) |
| Off-loop discovery | Skill and subagent disk discovery run on the blocking bridge, off the event loop | [architecture/skills-subagents.md](./architecture/skills-subagents.md) |
| Durable steering | `session.align()` queue snapshotted into every run checkpoint and restored on resume: in-order, at-least-once delivery across crash/resume; mid-turn resumes defer the drain past the re-executed tool batch | [usage/agent.md](./usage/agent.md#steering-an-in-flight-run) |
| GenAI semconv traces | `OpenTelemetryObserver` emits `gen_ai.*` semantic-convention attributes (operation, provider, conversation, cache tokens, tool call) alongside unchanged `linch.*` names | [usage/hooks.md](./usage/hooks.md#genai-semantic-conventions) |
| Scaffolding CLI | Stdlib-only `linch new` / `linch add tool` console script; generated projects run and test offline; core import graph untouched | [usage/cli.md](./usage/cli.md) |
| Proactive provider gate | Optional `Agent(limiter=...)` / `max_provider_concurrency=N` held around every live provider call (turn stream and compaction), released across retry backoff; cached provider clients rebuild when the event loop changes | [usage/extending.md](./usage/extending.md#limiter--gate-every-live-provider-call) |

### Deferred: central-loop structural split

The characterization tests that pin the loop's externally-observable order —
full event-type trace and full checkpoint-phase sequence per turn, alongside the
existing resume, hook-order, and terminal-result coverage — are in place
(`tests/loop/test_loop_trace_characterization.py`, `tests/loop/test_run_resume.py`,
`tests/test_hooks.py`). They are the prerequisite safety net for any future
refactor of `_run_loop_impl`.

The split itself is **deferred**. The naturally-separable pieces (`_drain_*`
helpers, `_SpanLifecycle`, `dispatch_*`, provider-snapshot save, worker recovery)
are already extracted; what remains is one state-coupled generator whose nested
closures share run-scoped mutable state. A mechanical split would thread that
state through a new interface for zero behavior change — and "smaller files alone
are not success." Reopen only when a concrete maintainability failure (not line
count) justifies it; the characterization net makes that safe when it does.

---

## Next candidates

Ranked prospects for the next slice, filtered through the
[acceptance gate](#roadmap-item-acceptance-gate). None is an active phase yet;
an item enters active work only with its evidence and completion criteria
pinned. Checked against the current tree before listing: thinking and
redacted-thinking blocks, image input, subagent context forking, durable
approvals, and budget/pricing already exist and do not belong here.

### Tier 1 — evidence-backed, ready to enter

**Gemini explicit context caching (`CachedContent`).**
Explicitly deferred from the prompt-cache slice. Anthropic/OpenAI implicit
prefix caching is instrumented and live-validated; Gemini is the one major
provider with no cache benefit. Mechanism: opt-in provider option that pins the
stable prefix as a `CachedContent` handle and reuses it across turns; the
advisory and report plumbing already exists. Compatibility: provider-scoped,
off by default. Completion: fake-backed tests for create/reuse/expiry plus a
live benchmark scenario mirroring the existing suite. Enters when a
Gemini-using embedder justifies it.

Two former Tier-1 items shipped and moved to
[Recently shipped](#recently-shipped): **durable steering** (mid-run steering
already existed as `session.align()`; the slice hardened it with checkpointed,
resume-safe, in-order at-least-once delivery) and **OTel GenAI
semantic-convention alignment** (additive `gen_ai.*` attributes on every span).

### Tier 2 — real value, needs design or a second embedder

- **Public session forking** — `agent.fork_session(session, at_seq=...)` for
  best-of-N sampling, A/B eval runs, and speculative exploration. The fork
  mechanics exist for subagents; the open design question is store semantics
  for the forked history (shared prefix vs. copy).
- **Anthropic cache-breakpoint tuning** — explicit `cache_control` placement at
  the last stable message to shrink the re-billed span after compaction
  (live-measured at ~79% warm versus ~99% baseline). Pure win with no behavior
  tradeoff, but a smaller audience than Tier 1.

### Considered and not queued

Batch-API eval mode (job polling drags deployment concerns into core),
streaming tool-argument deltas (a UI nicety with a thin audience), MCP
elicitation (the spec is still moving; wait for a reopen trigger), and the
central-loop structural split (deferred above; the characterization net is in
place for when a concrete maintainability failure appears).

---

## Cross-Cutting Verification

Every change runs the standard quality gates:

```bash
pytest
ruff check . && ruff format --check .
pyright
```

CI expands to:

- Correctness on Python 3.10, 3.11, 3.12, and 3.13.
- A minimal-core installation that proves optional integrations remain lazy.
- An all-extras smoke installation for MCP, Anthropic, Gemini, Postgres, and
  OpenTelemetry.
- Deterministic stress tests for concurrency, cancellation, resume, shutdown,
  close races, and crash boundaries.
- A fixed-runner performance job that records benchmark JSON and fails only on
  stable, agreed regression thresholds.

Each optimization must include a correctness oracle, not only a timing result.
Ranking, filtering, event ordering, checkpoint semantics, and cleanup are
verified independently from speed.

---

## What Linch Does Not Need Now

| Not in core | Boundary |
|---|---|
| Rust or a full native rewrite | Reopen only for a measured hotspot that survives Python optimization. |
| Coding-agent product UX or git workflow policy | Host app, project skill, coding-agent package, or external `IsolationBackend`. |
| Hosted dashboard, daemon, or supervisor | Host service, deployment platform, or separate event/report consumer. |
| Skill/tool marketplace | External registry or package manager with its own trust policy. |
| Large SaaS, cloud, browser, database, or shell tool bundles | Optional tool packages or MCP servers. |
| Vector database and embedding SDK dependencies | External `MemoryStore` adapters; examples may demonstrate the seam. |
| Domain-specific memory formats and prompts | Host-owned memory adapters, context builders, or presets. |
| Distributed queue, lease, or retry policy | Deployment-specific implementations behind existing protocols. |
| Default PII, PHI, or security classifier | Host governance hooks and verifiers. |
| Vendor-specific observability and authentication bundles | OpenTelemetry or external integration packages. |
| Bounded event queues by default | The async generator's natural backpressure remains the core contract. |

`deep_agent` remains an opt-in preset and distribution layer. Add behavior to it
only when the underlying capability is a reusable, domain-neutral mechanism.

### Reopen triggers

A deferred capability may return when at least one of these is true:

- Two unrelated embedders need the same mechanism and existing seams cannot
  express it cleanly.
- It can be a small protocol, optional method, hook, event, report field, or
  additive configuration rather than policy.
- It reduces core complexity or risk.
- It can be tested deterministically without a live service.
- Default behavior and replaceability remain intact.

---

## Roadmap Item Acceptance Gate

An item belongs in the active roadmap only when all answers are yes:

1. Does it help multiple kinds of embedded agents?
2. Is there repository evidence, a reproducible failure, or a benchmark that
   justifies it?
3. Is its compatibility behavior explicit for public APIs, protocols, events,
   defaults, and persisted data?
4. Can an embedder replace, disable, or ignore it without forking Linch?
5. Does it have deterministic correctness tests and measurable completion
   criteria?
6. Does it keep product policy and heavy vendor dependencies outside core?

When a phase meets its completion criteria, remove it from this active roadmap
and preserve its lasting contract in `docs/architecture/` or `docs/usage/`.
