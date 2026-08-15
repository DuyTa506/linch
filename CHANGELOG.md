# Changelog

Notable changes to `linch`. Versioning follows the contract in
[docs/versioning.md](docs/versioning.md): the public API is exactly `linch.__all__`,
and persisted wire formats are versioned separately via `linch.RUN_SCHEMA_VERSION`.

## Unreleased

### Dependencies

- `linch` now installs `jsonschema>=4,<5` as a core dependency for Canonical
  Tool Output V2. Environments pinned to jsonschema 3.x must update that pin
  before installing this release.

### BREAKING / Migration

- **Session construction and projections are migrating.** `Session` now uses
  one `session_log=` source of truth. The deprecated direct-construction
  `provider_view=`/`full_history=` compatibility shim remains for migration;
  new callers should use `SessionLog.seed(historical=..., visible=...)`.
  `session.provider_view` and `session.full_history` are deep-copied, read-only
  snapshots. Append with `await session.append(...)` or
  `session.session_log.append(...)`, and record compaction with
  `session.session_log.record_projection(...)` rather than mutating either
  projection.
- **Projection durability has a defined boundary.** `MessageEntry` and
  `ProjectionEntry` are in-memory log entries; session stores persist the
  message/projection snapshots needed to reconstruct the views, not a second
  projection journal. Per-request context-builder output remains ephemeral and
  is outside the session log.
- **Agent context ownership is explicit.** `Agent(context=...)` adopts and
  disposes the supplied scope during `close()`. Hosts retaining a parent scope
  should pass a child from `parent.scope(...)`.
- **Execution backends are unified.** New integrations should pass an
  `ExecutionBackend` exposing coherent `.shell` and `.fs` transports (use
  `LocalExecutionBackend` or `RemoteExecutionBackend`). A shell-only
  `execution_backend` remains a deprecated compatibility path and emits
  `DeprecationWarning`; it has no filesystem transport unless `filesystem=` is
  explicit. Migrate before enabling `FeatureFlags(filesystem=True)`.
- **Execution confinement and cwd are declarative.** Non-empty confinement
  metadata records the embedder's claim; Linch does not verify that boundary.
  The effective session cwd is sent to the shell transport, and a custom world
  must map it consistently with filesystem paths.
- **Pipeline authorization ordering is fixed.** `ToolPipeline` runs after
  canonical validation and permission. Its listeners must not rewrite the
  authorization-sensitive tool, input, or decision; use `PreToolUse` for a
  transform that is validated and authorized again.
- **Tool registration semantics are exact.** `ToolRegistry.register` now
  rejects duplicate names; use `replace` for a reversible hot swap. Await the
  returned `Disposable` (`await disposer.dispose()`), which removes only its
  identity-owned registration. A successful disposer is single-use; failed
  teardown remains retryable.
- **Required persisted data is strict on read.** Unknown or malformed required
  events fail loading. Only producer-declared `ignorable` events may be read as
  `IgnorableEvent` and skipped.

### Added

- New public kernel and capability names: `Context`, `Disposable`,
  `ExecutionBackend`, `ShellBackend`, `LocalExecutionBackend`,
  `RemoteExecutionBackend`, `ToolExecution`, `ToolPipeline`, and `SessionLog`.
  `IgnorableEvent` and `is_ignorable_event` are public for forward-compatible
  event readers. `ToolPipeline` exposes the
  `tools/pre-execute → tools/execute → tools/post-execute` lifecycle; active
  pipeline listeners and execution worlds contribute stable durable resume
  identity. The shipped `tools/execute` wrappers `metrics_wrapper` and
  `timeout_wrapper` are public as well, so embedders can compose them without
  importing a private submodule path.

- **Cheap projection reads.** `SessionLog.visible_count`, `.history_count`, and
  `.last_visible()` (plus `Session.last_provider_message()`) answer count and
  newest-message questions without snapshotting the conversation. Reading
  `provider_view`/`full_history` returns a detached deep copy by design, so
  callers that only need a length or the last turn should use these instead —
  the loop, checkpointing, and compaction now do.

### Fixed

- `DockerBackend` now declares `confinement`, so an agent configured with it
  again reports Bash as sandboxed in the system prompt. The tri-state
  sandbox reporting introduced with the execution seam requires an explicit
  confinement declaration, which Linch's own Docker backend did not make —
  it was described to the model as unverified. Durable resume identity is
  unchanged: a shell-only backend still fingerprints through its
  `resume_policy_config`, which does not include confinement.

- **Incremental durability ledger.** `Agent(durability=...)`,
  `DurabilityOptions`, and `DurabilityOptions.strict_v1()` opt into a durable
  next-turn inbox and exact pending model input. Public `InboxDelivery` and the
  optional `SessionInboxStore` support delivery-ID deduplication through the
  new `Session.notify()` method when `durable_inbox=True`. `ModelInputSnapshot`,
  `ModelInputSnapshotError`, `ModelInputSnapshotStore`,
  `create_model_input_snapshot()`, and `decode_model_input_snapshot()` expose
  crash-safe provider-request replay. In-memory implementations preserve the
  same semantics only within one process; SQLite stores survive restarts, and
  Postgres implements the session inbox capability. Each enabled mode fails
  closed when a configured custom store lacks its corresponding optional inbox
  or snapshot protocol.
- **Leased coordination delivery.** Public `ClaimableMailbox`, `MailboxClaim`,
  `LeasedScheduleStore`, and `ScheduleOccurrence` add claim/ack/release leases
  with stable delivery IDs. The built-in in-memory and SQLite mailbox/schedule
  stores support the new protocols while the legacy `send()`/`drain()` and
  `claim_due()` paths remain available.
- **Canonical Tool Output V2.** Public `JsonValue`, `ToolOutput`,
  `ToolOutputError`, and `ToolAttachment` types support schema-validated JSON
  tool results. `@tool(...)` accepts `output_schema`, `render_output`,
  `renderer_id`, and `renderer_version`; `ToolCallEndEvent.tool_output` carries
  the optional canonical value while all legacy result projections remain.
  Validation uses JSON Schema Draft 2020-12.
- Transactional `Agent`/`Session` shutdown stops new admissions, waits for live
  provider/tool/iterator work, coalesces concurrent close calls, and permits a
  later close to retry unfinished teardown after an error.
- Background worker audit events carry their origin. By default, detached
  result notifications remain process-local. With `durable_inbox=True` and a
  capable session store, a completion that reaches `Session.notify()` is
  durably queued and deduplicated for the next turn. Linch still does not
  reconstruct a detached task that was in flight when the process stopped.

## 2.0.0 — 2026-08-13

Linch 2.0 is a breaking release for the SDK runtime defaults. It keeps Linch
as a reusable harness; a coding agent remains an explicit application/preset,
not the SDK's implicit identity.

### BREAKING

- **Neutral `Agent` defaults.** A bare `Agent` has an empty tool registry, a
  domain-neutral system identity, and `FeatureFlags` disabled for skills,
  subagents, MCP, and filesystem discovery. Pass `tools=workspace_tools()` (or
  your own registry), enable trusted features explicitly, or use
  `create_deep_agent(...)` for the opt-in deep-agent preset. `default_tools()`
  remains a compatibility alias for the workspace preset but is no longer an
  implicit `Agent` default.
- **A bare `Agent` no longer writes conversation state under the working
  directory.** Its implicit session store changed from
  `.linch/sessions.db` to `InMemorySessionStore`, so history no longer survives
  a process restart unless the host supplies `session_store=` explicitly. Use
  `SqliteSessionStore` or another persistent session store in services that
  require restart durability.
- **Read-before-write is now opt-in on a bare `Agent`.** The
  `read_before_write` default changed from `True` to `False` with the neutral
  runtime defaults. Set `read_before_write=True` for custom/workspace tool
  configurations that still require the virtual-filesystem edit-after-read
  guard; `create_deep_agent()` enables it for its workspace preset.
- **The deep-agent factory is bounded by default.**
  `create_deep_agent()` now selects `profile="balanced"`, which caps the shared
  agent/subagent tree at 64 turns and 1,000,000 tokens. Select
  `profile="unbounded"` only when the embedding service supplies equivalent
  lifetime and cost controls.
- **Permission input is canonical before approval.** Pre-tool transformations
  are validated and permission-checked again. The old approval callback
  `updatedInput` response is rejected; mutate in `PreToolUse` instead.
- **Provider stream boundary is strict.** Provider adapters must emit Linch's
  normalized event vocabulary and required fields; raw vendor objects and
  malformed events are not accepted by the loop.
- **Durable Docker runs with environment forwarding require a fingerprint
  secret.** For a durable run that offers a Docker-backed `Bash` tool, a
  non-empty `DockerBackend.env` or `DockerBackend.forward_env` now requires
  `resume_fingerprint_key` as `bytes` with at least 16 bytes. Linch uses the key
  to HMAC environment values into the run contract without persisting the
  values themselves. Supply the same protected key on every host that may
  resume the run; configurations without Docker environment values are
  unaffected.

### Added

- `workspace_tools()` and explicit deep-agent profiles (`DeepAgentProfile` /
  `DEEP_AGENT_PROFILES`) for callers that want a ready software-workspace
  catalog without making it an SDK default. The new built-in verification
  subagent is intentionally read-only and does not offer Bash; prompt
  instructions alone cannot enforce that boundary.
- `ToolContext.report_progress()` and `ToolProgressEvent`, a best-effort,
  observational progress channel that never enters provider history or the
  durable run event log.
- `RunContract`, canonical fingerprint helpers, and
  `RunContractMismatchError` for fail-closed durable-run checks. Verification
  runs when a durable run is created as well as when it resumes. Custom
  callbacks and policy-bearing hooks require a stable `resume_policy_id`;
  custom Bash backends require both stable identity and non-`None`, JSON-safe
  policy configuration. Unverifiable runs are rejected before persistence.
  Legacy runs without a contract require
  `RunOptions(allow_legacy_resume=True)` for an explicit migration override.
- Portable session forking through the public `Agent.fork_session(...)`
  surface. Forks copy a validated history prefix and metadata, not live work
  or arbitrary application state.
- Provider-agnostic compaction/snapshot recovery and concurrency-safe SQLite /
  Postgres storage allocation. In 2.0.0, background worker audit events carried
  their origin, but detached result notifications remained process-local and
  durable delivery belonged to the embedding application. The opt-in ledger
  described under Unreleased adds durable delivery after enqueue, not recovery
  of a worker that was still running when its process stopped.

### Fixed

- A duck-typed abort signal whose `wait()` completed spuriously could cancel a
  live permission callback. Linch now waits until the signal reports an actual
  abort.
- Closing or abandoning a serial or parallel tool event stream now cancels and
  joins every tool task it started instead of allowing detached execution to
  leak past the stream lifetime.
- Durable run-contract JSON normalization now rejects recursive/deep values and
  non-string or colliding mapping keys instead of recursing indefinitely or
  silently merging distinct authority configuration.
- A context hook can no longer re-widen the per-turn offered-tool boundary;
  the resolved request is intersected with the current turn's allowed tools.
- SQLite run failure records now isolate checkpoint/meta values consistently,
  and session task-ID allocation resumes above existing rows under concurrent
  creation instead of reusing hard-coded counters.
- Model-produced terminal-tool batches raise `ProviderError`, and successful
  stop acknowledgements are recorded as non-error tool results.
- Compaction retries preserve the selected fallback model, active
  checkpointable hooks clear stale state they no longer own, legacy non-list
  error metadata is retained when a new failure is appended, and provider
  `stop_reason` values must be strings from the normalized vocabulary.

See [migration-2.0.md](docs/migration-2.0.md) for examples and the complete
upgrade checklist.

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
