# Key Invariants

> Part of the [Linch architecture guide](./README.md).

These must not break across refactors:

| # | Invariant |
|---|---|
| 1 | **`full_history` is append-only** — only the `loop/` package appends; never write to it elsewhere. |
| 2 | **`provider_view` is the only thing compaction mutates** — `full_history` is untouched. |
| 3 | **Tool protocol is duck-typed** — no base class, no `isinstance`; check attribute presence. |
| 4 | **`stream()` yields normalized dicts** — the loop must not import any provider's raw types. |
| 5 | **Default SWE system-block text is pinned** — `test_system_blocks.py` has a byte-identical parity assertion; update it intentionally. |
| 6 | **`final_tool_name` tool is never scheduled** — the loop intercepts before the scheduler. |
| 7 | **Context builders do not mutate history** — they receive a `provider_view` snapshot and return ephemeral request context. |
| 8 | **`run_deps` is set once per `run_loop` call** — at the top, from `opts.deps ?? agent.deps`. |
| 9 | **Loop guard is on by default** — `Agent()` without `loop_guard=` gets `LoopGuard()` with safe thresholds; disable explicitly with `Agent(loop_guard=None)`. |
| 10 | **Provider capabilities apply per-request** — `loop/request.py` always calls `apply_provider_capabilities()` when the provider has `capabilities()`; no provider receives features it declared unsupported. |
| 11 | **Offload only replaces `ToolResult.content` before block construction** — the full result is preserved on `ToolCallEndEvent.tool_result`; `full_history` and `provider_view` receive the preview only. `maybe_offload` never raises — a backend write failure silently returns the original result so a storage hiccup never breaks a run. |
| 12 | **Filesystem tools are excluded from offloading** — `read_file`, `write_file`, `edit_file`, `ls` are in `OffloadConfig.skip_tools` by default; reading a large file back cannot trigger a recursive re-offload. |
| 13 | **Background workers are cancelled at both exit paths** — `_cancel_background_workers(session)` is called in both the `except AbortError` and `except Exception` handlers in `run_loop`. Worker tasks must never write into a session whose run has already ended. |
| 14 | **`SubagentTool` always uses `retain=True`** — child sessions remain in `agent._sessions` until `agent.close()` clears them. `continue_subagent` relies on the child session being live; removing it would silently break fork/continue. |
| 15 | **`session.pending_notifications` is drained before `ContextInjectionHook.build_context()`** — `_drain_pending_notifications` converts each queued `<task-notification>` Message into a `UserEvent` at the top of every new turn, so the model sees background task results before the next provider call. |
| 16 | **One `RunBudget` object per agent tree** — children inherit the parent's `active_budget` by reference in `run_subagent`; charging happens only in `run_loop` next to the `UsageEvent`. Never copy a budget into a child. |
| 17 | **`micro_compact` is copy-on-write** — provider-view messages/blocks are shared with `full_history`; elision must build new `Message`/`ToolResultBlock` objects, never mutate in place. With `compaction_ladder=None` the compaction/retry event sequence is byte-identical to the pre-ladder code (pinned by `test_ladder_disabled_is_byte_identical`). |
| 18 | **Workflow journal records are append-only `WorkflowEvent`s in the run store** — replay correctness depends on `agent_end`/`agent_replayed` events being persisted for every completed `wf.agent` call; do not emit them for failed calls. |
| 19 | **Persisted wire formats are versioned and forward-tolerant** — `checkpoint_to_dict` stamps `schema_version` (`linch.RUN_SCHEMA_VERSION`); `checkpoint_from_dict` reads field-by-field with defaults (a newer checkpoint round-trips its known fields), and `load_events` skips an undecodable event row rather than aborting the resume. Bump `SCHEMA_VERSION` only on a breaking shape change. |
| 20 | **The public API surface is exactly `linch.__all__`** — `tests/test_public_api.py` enforces that every name resolves, there are no duplicates, and no public (non-underscore) attribute leaks onto the package undeclared. Changing the surface is a deliberate, reviewable edit; `docs/versioning.md` is the semver contract. |
| 21 | **The event stream is a plain async generator** — `session.run()`/`resume()` `yield` events directly from `run_loop`; there is no unbounded internal queue, so a slow consumer applies backpressure to the whole producer. Do not wrap the loop in an unbounded buffering task. |
| 22 | **No process-global mutable state** — every `Agent` builds its own registries/stores/engines so N agents are multi-tenant-isolated; `session.abort()` and `agent.close()` both drain background-worker *and* background-tool tasks. |

## Design rationale

These are written down (and several are pinned by tests) on purpose:

- **Invariants are a contract, not folklore.** Many recur as crosscutting assumptions
  the loop, compaction, subagents, and resume all depend on — enumerating them means a
  refactor can be checked against an explicit list instead of rediscovering each rule by
  breaking it.
- **The load-bearing ones are pinned by byte-identical tests.** The default system
  prompt (#5), the ladder-disabled event sequence (#17), and the public API (#20) assert
  exact equality, so accidental drift fails CI — a change has to be a deliberate
  edit-plus-update.
- **"Byte-identical when the feature is off" is a recurring design choice for a
  reason.** Opt-in features (compaction ladder, offload, budgets, coordination) must add
  zero observable behavior when unused, so existing users can upgrade without surprises
  and the cost of a feature is paid only by those who turn it on.

---

Back to the [architecture index](./README.md).
