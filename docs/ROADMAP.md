# Linch SDK Roadmap

This file is intentionally kept as a lightweight roadmap/status page.

---


## Future Development Directions

Linch should keep its center of gravity as an explicit, embeddable runtime for
context-heavy agent workflows. It does not need to copy every part of a broader
agent ecosystem SDK. The strategic advantage is runtime transparency: clear
context construction, policy-aware tool execution, inspectable events,
resource-aware scheduling, durable state, and a small surface that application
developers can embed without surrendering control.

---

## Phase 4 — Budgets, compaction ladder, workflow engine (2026-06)

- **A. Budget primitive** — `RunBudget` token/USD caps shared across the agent tree;
  `BudgetEvent` warning/exceeded; graceful error stop.
  Primary files: src/linch/budget.py, src/linch/loop/, src/linch/session.py,
  src/linch/subagents/runner.py, src/linch/events.py
- **B. Compaction ladder** — micro-compact (LLM-free tool-result elision), reactive
  recovery on ContextLengthError, forced-compaction circuit breaker; opt-in via
  `Agent(compaction_ladder=CompactionLadder())`.
  Primary files: src/linch/compaction.py, src/linch/loop/
- **C. Workflow engine** — `agent.run_workflow(fn)`: WorkflowContext
  (agent/parallel/pipeline/phase/budget), content-addressed journal on RunStore,
  resume replay, WorkflowEvent.
  Primary files: src/linch/workflow/{context,journal,engine}.py, src/linch/events.py,
  src/linch/agent.py

---

## Phase 5 — Loop package split + unified hooks extension layer (2026-06)

- **A. Loop package split** — `loop.py` decomposed into a `loop/` package by
  responsibility (runner, streaming, request assembly, terminal tails/gates,
  checkpointing); public surface re-exported so `from linch.loop import ...` is
  unchanged.
  Primary files: src/linch/loop/{runner,streaming,request,terminals,checkpoint,__init__}.py
- **B. Closed-loop verification gates** — structured-output schema repair
  (`structured_output_retries`), final-answer verifier protocol + `ScorerVerifier`
  bridge, and a `stop_when`-style stop predicate; all opt-in and bounded by
  `max_turns` / the run budget.
  Primary files: src/linch/verification.py, src/linch/loop/terminals.py
- **C. Unified hooks layer** — a single `Agent(hooks=[...])` extension surface
  replacing the separate `observers` / `middleware` / `context_builder` /
  `verifiers` / `stop_when` parameters. Typed `HookEvent` chokepoints
  (agent/turn/provider/tool/final-answer/stop/subagent/event), `HookResult`
  actions (continue/mutate/block/retry/stop/force_continue), a fan-out
  `HookDispatcher` with per-hook telemetry (`HookEventRecord`), and built-in
  adapters: `ContextInjectionHook`, `ToolMiddlewareHook`, `FinalAnswerVerifierHook`,
  `StopPredicateHook`, `RunTelemetryHook`.
  Primary files: src/linch/hooks/{types,contexts,dispatcher,adapters,__init__}.py,
  src/linch/loop/runner.py, src/linch/scheduler.py, src/linch/session.py,
  src/linch/agent.py
- **D. Runtime hardening** — final-tool retries pair the terminal `tool_use` with
  a synthetic `tool_result`; tool middleware fails closed; PostToolUse mutations
  preserve large-result offload; AgentStop/observer-close fire on every terminal
  path; multi-hook context is globally budgeted; blocking disk/SQLite/sync-tool
  work runs on a bounded daemon-thread offload (`_blocking.run_blocking`) so the
  event loop is never blocked.
  Primary files: src/linch/loop/{runner,terminals,request}.py, src/linch/scheduler.py,
  src/linch/hooks/adapters.py, src/linch/_blocking.py, src/linch/filesystem/disk.py,
  src/linch/storage/_executor.py, src/linch/tools/function.py

---

## Docs

Topic-split usage guide under `docs/usage/` (agent, providers, events, tools,
structured-output, hooks, context-and-memory, filesystem, workflows, deep-agent,
skills, examples); `docs/usage.md` retained as a redirect index.
