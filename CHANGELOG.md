# Changelog

Notable changes to `linch`. Versioning follows the contract in
[docs/versioning.md](docs/versioning.md): the public API is exactly `linch.__all__`,
and persisted wire formats are versioned separately via `linch.RUN_SCHEMA_VERSION`.

## 1.2.0 — 2026-08-11

### Added

- **`wf.step(name, fn, *, key=None)`** — run any zero-argument sync or async callable
  as a journaled workflow step. Previously only `wf.agent` was journaled, so any other
  side-effecting code in a workflow function re-executed on every resume. Wrapping it in
  a step makes it happen exactly once across a run, no matter how many times the run is
  resumed. The step's `name` is its durable identity; pass `key=` to journal the same
  step over different inputs separately. Values are journaled as JSON, so a
  non-serializable return raises `ConfigError` — including when no run store is
  configured, so the failure surfaces in development rather than in production.
- Three `WorkflowEvent` kinds: `step_start`, `step_end`, `step_replayed`. Additive and
  fail-safe on older readers (an unknown kind decodes as `phase` and the step simply
  re-executes), so `RUN_SCHEMA_VERSION` is unchanged.
- `linch.events.WORKFLOW_EVENT_KINDS` and `linch.workflow.journal.JOURNALED_KINDS` —
  the kind list and its journaled subset, previously hard-coded in three places.
- `linch.workflow.step_key` — the step call-key hash, domain-separated from `call_key`.
- **`wf.interrupt(key, payload=None)`** — durable human-in-the-loop. With no answer, the
  workflow emits `interrupt_requested`, is checkpointed as `"suspended"` (not failed) and
  raises `WorkflowSuspended`; re-invoking the same `run_id` with `resume={key: value}`
  replays the journaled prefix and returns the answer, which is itself journaled so later
  resumes never ask again. `WorkflowSuspended` extends `BaseException` so a workflow's own
  `except Exception:` cannot swallow a suspend. A `resume` entry is consumed once per key.
  Known limitation: interrupting inside a `wf.parallel` branch cancels its siblings.
- **`wf.settled(thunks) -> list[StepOutcome]`** — like `wf.parallel`, but a failing branch
  leaves its siblings running. Each `StepOutcome` carries `.ok` / `.value` / `.error`, in
  input order. Cancellation and suspension propagate instead of becoming outcomes.
- **`timeout_ms` and `retry` on `wf.agent` / `wf.step`**, with `step_timeout_ms` as the
  workflow-wide default (`0` opts a call out). An expired call raises the new
  `WorkflowTimeoutError`. The timeout applies per attempt, so a retry gets a full budget;
  only the winning attempt is journaled, and a `ConfigError` is never retried.
- **`run_workflow(deadline_ms=...)`** — a wall-clock cap on the whole workflow. The
  journaled prefix survives, so a resume picks up where it stopped.
- **`run_workflow(signal=...)`** — an `AbortContext` that stops the workflow at its next
  journaled call and now actually propagates into every subagent it spawns.
- **`run_workflow(max_agent_concurrency=...)`** — caps how many `wf.agent` calls hold a
  live provider slot at once, independent of `max_concurrency`'s fan-out shape. It never
  gates a replayed call.
- **`run_workflow(journal_snapshot_every=N)`** — checkpoints the journal into
  `RunCheckpoint.extension_state["linch.workflow"]` every N records so a resume folds only
  the events after it. Off by default; a missing, oversized or malformed snapshot falls
  back to folding the whole event log.
- Six more `WorkflowEvent` kinds: `interrupt_requested`, `interrupt_resolved`,
  `interrupt_replayed`, and the `step_*` trio above.
- `StepOutcome`, `WorkflowSuspended` and `WorkflowTimeoutError` are exported from `linch`;
  `linch.workflow.interrupt_key` joins `step_key` and `call_key`.
- `WorkflowJournal.snapshot()` / `from_stored_events(snapshot=...)`, and
  `providers.retry.with_retry(retry_on=...)` for deciding retryability by predicate
  instead of the exception's `retryable` attribute.
- **`CheckpointableHook`** — an opt-in protocol letting a hook persist and restore its own
  state across a resume, through the new `RunCheckpoint.extension_state`: an opaque,
  JSON-safe map of extension-owned namespaces that core preserves without interpreting.
  It is omitted from the serialized checkpoint when empty, so a run that installs no such
  hook writes a byte-identical payload to before.
- **`RunOptions.stream_partials`** — suppress the raw `PartialAssistantEvent` projection
  for one run while still consuming and assembling the provider's deltas. `None` keeps the
  agent-level setting.

`WorkflowJournal.record()` gained an optional `record_kind` argument (default `"agent"`),
and `CURRENT_FINGERPRINT_VERSION` is deliberately **unchanged at 2** — step keys live in
a disjoint hash domain, so introducing `wf.step` does not invalidate the journal of any
run already in flight.

### Fixed

- **`wf.parallel` leaked work after a branch failed.** It used a bare `asyncio.gather`,
  so a raising branch propagated its error while its siblings kept running to completion
  — burning budget, and appending journal events to a run that had just been marked
  failed and whose host session was already released. Remaining branches are now
  cancelled and drained before the error propagates. Cancelled branches are not
  journaled and re-run on resume.
- **Nested `wf.parallel` / `wf.pipeline` could deadlock permanently.** The branch
  semaphore was not re-entrant, so a fan-out nested inside a `wf.parallel` branch waited
  on slots its own caller was holding whenever the outer fan-out was at least as wide as
  `max_concurrency`. With no workflow-level timeout, this hung silently. Each nesting
  level now gets its own budget; a single-level fan-out is unchanged.
- **A workflow could not be aborted, only task-cancelled.** `WorkflowContext` never set
  `RunSubagentArgs.signal`, so an `AbortContext` had no effect on a running workflow. It
  is now threaded through `run_workflow(signal=...)` into every journaled call and every
  subagent.
