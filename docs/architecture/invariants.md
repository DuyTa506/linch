# Key Invariants

> Part of the [Linch architecture guide](./README.md).

These must not break across refactors:

| # | Invariant |
|---|---|
| 1 | **`full_history` is append-only** — only the `loop/` package appends; never write to it elsewhere. |
| 2 | **`provider_view` is the only thing compaction mutates** — `full_history` is untouched. |
| 3 | **Tool protocol is duck-typed** — no base class, no `isinstance`; check attribute presence. |
| 4 | **`stream()` yields normalized dicts** — the loop must not import any provider's raw types. |
| 5 | **The bare SDK prompt is neutral and tool-truthful** — `Agent()` has no implicit coding identity or workspace tools. Coding doctrine belongs to explicit workspace/deep-agent presets, and every prompt clause must describe only tools offered to that request. |
| 6 | **`final_tool_name` tool is never scheduled** — the loop intercepts before the scheduler. |
| 7 | **Context builders do not mutate history** — they receive a `provider_view` snapshot and return ephemeral request context. |
| 8 | **`run_deps` is set once per `run_loop` call** — at the top, from `opts.deps ?? agent.deps`. |
| 9 | **Loop guard is on by default** — `Agent()` without `loop_guard=` gets `LoopGuard()` with safe thresholds; disable explicitly with `Agent(loop_guard=None)`. |
| 10 | **Provider capabilities apply per-request** — `loop/request.py` always calls `apply_provider_capabilities()` when the provider has `capabilities()`; no provider receives features it declared unsupported. |
| 11 | **Filesystem/offload are explicit** — a bare `Agent` has `FeatureFlags(filesystem=False)`, no virtual-filesystem tools, and no offload path. `FeatureFlags(filesystem=True)` is required before a backend/offload configuration can affect a run. |
| 12 | **Enabled offload only replaces `ToolResult.content` before block construction** — the full result is preserved on `ToolCallEndEvent.tool_result`; `full_history` and `provider_view` receive the preview only. `maybe_offload` never raises — a backend write failure silently returns the original result so a storage hiccup never breaks a run. |
| 13 | **Filesystem tools are excluded from enabled offloading** — `read_file`, `write_file`, `edit_file`, `ls` are in `OffloadConfig.skip_tools` by default; reading a large file back cannot trigger a recursive re-offload. |
| 14 | **Background tasks are cancelled on abort/close, not a normal completion** — a normal background-tool completion enqueues its notification before completion audit. Delivery is in-process by default; with `DurabilityOptions(durable_inbox=True)` an already-enqueued completion survives restart. An executing task itself is not recovered. |
| 15 | **`SubagentTool` always uses `retain=True`** — child sessions remain in `agent._sessions` until `agent.close()` clears them. `continue_subagent` relies on the child session being live; removing it would silently break fork/continue. |
| 16 | **Next-turn notifications are drained before `ContextInjectionHook.build_context()`** — legacy mode drains `session.pending_notifications`; durable mode atomically commits the session inbox. Direct legacy-queue mutation is rejected in durable mode. A frozen `provider_pending` resume is the same turn, so new delivery waits for the following turn. |
| 17 | **One `RunBudget` object per agent tree** — children inherit the parent's `active_budget` by reference in `run_subagent`; charging happens only in `run_loop` next to the `UsageEvent`. Never copy a budget into a child. |
| 18 | **`micro_compact` is copy-on-write** — provider-view messages/blocks are shared with `full_history`; elision must build new `Message`/`ToolResultBlock` objects, never mutate in place. Every successful proactive or reactive provider-view compaction persists a replacement snapshot when the store supports snapshots. With `compaction_ladder=None` the compaction/retry event sequence is byte-identical to the pre-ladder code (pinned by `test_ladder_disabled_is_byte_identical`). |
| 19 | **Workflow journal records are append-only `WorkflowEvent`s in the run store** — replay correctness depends on `agent_end`/`agent_replayed` (for `wf.agent`), `step_end`/`step_replayed` (for `wf.step`) and `interrupt_resolved`/`interrupt_replayed` (for `wf.interrupt`) events being persisted for every completed call; do not emit them for a failed call *or a failed retry attempt* — an exhausted node must re-run on resume, not replay a failure. The journaled subset is `JOURNALED_KINDS` in `workflow/journal.py`; keep it in lockstep with `WORKFLOW_EVENT_KINDS` in `events.py`. A journal snapshot in `RunCheckpoint.extension_state["linch.workflow"]` is only an accelerator: it must stay optional, and a missing or malformed one falls back to folding the whole event log. |
| 20 | **Persisted run state is versioned, forward-tolerant, and contract-bound** — checkpoints stamp `linch.RUN_SCHEMA_VERSION` and decode field-by-field; undecodable event rows are skipped rather than aborting resume. A `RunContract` independently fingerprints resolved execution semantics; resume fails closed for a missing legacy contract or a mismatch unless the caller explicitly opts into the unsafe legacy path. |
| 21 | **The public API surface is exactly `linch.__all__`** — `tests/test_public_api.py` enforces that every name resolves, there are no duplicates, and no public (non-underscore) attribute leaks onto the package undeclared. Changing the surface is a deliberate, reviewable edit; `docs/versioning.md` is the semver contract. |
| 22 | **The event stream is a plain async generator** — `session.run()`/`resume()` `yield` events directly from `run_loop`; there is no unbounded internal queue. Per-tool progress is a bounded, coalesced stream projection, never provider context or terminal state. |
| 23 | **No process-global mutable state** — every `Agent` builds its own registries/stores/engines so N agents are multi-tenant-isolated; a bare agent uses an in-memory session store and does not create project-local filesystem state. `session.abort()` and `agent.close()` drain background-worker and background-tool tasks. |
| 24 | **Permission decisions authorize only canonical, offered calls** — resolve/validate → offered-tool boundary → `PreToolUse` → revalidate → final permissions → execute. Unoffered calls never reach hooks or policy fallback. Persisted approvals are keyed by the final canonical input and cannot authorize a transformed or unoffered call. |
| 25 | **Exact model input is opt-in and fail-closed** — every enabled provider attempt snapshots the final effective `ProviderRequest` before dispatch. A pending resume uses that snapshot without rerunning context or request-mutation hooks; missing, corrupt, or unsupported snapshots never fall back to a rebuilt request. |
| 26 | **Tool Output V2 is opt-in per tool** — only a tool declaring `output_schema` enters canonical JSON normalization, Draft 2020-12 validation, and rendering. Legacy tools and their event/provider projections retain their existing behavior. |

## Design rationale

These are written down (and several are pinned by tests) on purpose:

- **Invariants are a contract, not folklore.** Many recur as crosscutting assumptions
  the loop, compaction, subagents, and resume all depend on — enumerating them means a
  refactor can be checked against an explicit list instead of rediscovering each rule by
  breaking it.
- **The load-bearing ones are pinned by exact tests.** The default system prompt (#5)
  has targeted protocol assertions, while the ladder-disabled event sequence (#18) and
  the public API (#21) assert exact equality. Accidental drift therefore fails CI and
  requires a deliberate edit-plus-update.
- **"Byte-identical when the feature is off" is a recurring design choice for a
  reason.** Opt-in features (compaction ladder, offload, budgets, coordination) must add
  zero observable behavior when unused, so existing users can upgrade without surprises
  and the cost of a feature is paid only by those who turn it on.

---

Back to the [architecture index](./README.md).
