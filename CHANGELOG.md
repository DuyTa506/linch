# Changelog

Notable changes to `linch`. Versioning follows the contract in
[docs/versioning.md](docs/versioning.md): the public API is exactly `linch.__all__`,
and persisted wire formats are versioned separately via `linch.RUN_SCHEMA_VERSION`.

## 1.2.1 — 2026-08-12

### BREAKING (dependency floor)

- **`linch[mcp]` now requires `mcp>=2.0.0`** and no longer supports mcp 1.x.
  mcp 2.0 renamed `streamablehttp_client` to `streamable_http_client`, removed
  its `headers=` kwarg in favour of a caller-supplied `httpx2.AsyncClient`,
  changed the transport from a 3-tuple to a 2-tuple yield, and switched its
  pydantic models to snake_case fields (`is_error`, `input_schema`,
  `read_only_hint`, `destructive_hint`, `mime_type`). `linch.mcp` targets the
  2.x API.

  This lands in a PATCH because no name in `linch.__all__` changed — see
  [docs/versioning.md](docs/versioning.md#optional-extra-dependencies-are-not-covered),
  which does not cover an extra's dependency floor. **If you are held on mcp 1.x,
  pin `linch<=1.2.0`.** Nothing else here requires action: your server config is
  unchanged, and `McpServerConfig.headers` still works — it now rides on the
  httpx client linch builds and owns.

### Added

- **`Agent(limiter=...)`** — a duck-typed `Limiter` protocol
  (`async acquire(*, model)` / `release(*, model)`) that core holds around every
  live provider call: the turn stream and `strategy.compact(...)`. The gate sits
  inside the turn generator, so it is released while a same-model retry backs off
  and released deterministically when a caller abandons a run. Use it for a shared
  budget across agents, per-model quotas, or a token bucket.
- **`Agent(max_provider_concurrency=N)`** — shortcut that builds a
  semaphore-backed limiter, so there is exactly one enforcement path. Passing both
  it and `limiter=` raises `ConfigError`. Unlike a semaphore around `agent.run()`,
  this bounds the provider calls a run fans out into: every turn, retry,
  model-fallback swap, compaction summarization, and subagent. The cap survives
  an `Agent` being reused from a second event loop: `asyncio.Semaphore` binds to
  the loop of its first *contended* acquire, so the semaphore is rebuilt when the
  loop changed and nothing is held. Rebuilding while slots are still outstanding
  would give the second loop its own full budget, so that raises `ConfigError`
  instead.

### Fixed

- **A cancelled MCP connect leaked its resources.** The unwind caught
  `Exception`, which does not include `asyncio.CancelledError`, so a connect
  interrupted by a shutdown or a timeout stranded the stdio subprocess, the
  `ClientSession`, and the httpx client. Cancellation now releases everything
  entered so far and propagates unchanged instead of surfacing as a
  `ConfigError`. Cleanup is best-effort: a second cancellation arriving mid-unwind
  can still cut it short.
- **MCP tool input schemas were being discarded.** `to_input_schema` read
  `.properties`/`.required` as attributes, but `Tool.input_schema` is a plain
  JSON Schema dict, so every MCP tool reached the model as
  `{"type": "object", "properties": {}}` — no arguments, no `required`. The
  schema now passes through intact. Only the unit tests' fake `mcp` modules,
  which supplied attribute-shaped schemas, had ever matched the old code path.
- Cached provider clients (`OpenAIChatCompletionsProvider` and its
  vLLM/SGLang/llama.cpp/DeepSeek subclasses, `AnthropicProvider`,
  `OpenAIResponsesClient`) are rebuilt when used from a different event loop than
  the one they were constructed on. Reusing a provider across loops — the normal
  shape of a Celery worker, which runs one loop per task — previously raised
  `Event loop is closed` and forced hosts to keep their own loop-keyed provider
  cache.

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
