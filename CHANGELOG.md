# Changelog

Notable changes to `linch`. Versioning follows the contract in
[docs/versioning.md](docs/versioning.md): the public API is exactly `linch.__all__`,
and persisted formats share a wire generation separate from the package version,
reflected by `linch.RUN_SCHEMA_VERSION`.

## Unreleased

- Check shipped example imports and run commands against the supported surface.
  Correct supported imports, Glob inputs and registration in the budget demo,
  interruption before durable resume, empty subagent tool-list display, and the
  provider template's capability claims. Private-helper imports remain flagged
  for an owner decision rather than expanding the public API.

- Export `ProviderRequest`, `SystemBlock`, `BashTool`, and `DockerBackend` lazily
  from `linch`. These four additions are an owner-approved exception to the
  MINOR rule in `docs/versioning.md`, as with `Agent.reload_skills()` in 3.0.1.

- Keep each MCP server's transport and session contexts in one dedicated owner
  task so stdio and HTTP connections can close safely from any task, including
  `Agent.close()`. Drain startup failures, cancellation, and concurrent shutdowns.

- Add usage-page use cases, exact API headings with preserved anchors, and
  source-checked troubleshooting for configuration, tools, authentication, and resume.

- Check imports and Python syntax across shipped docs, document supported optional
  integration modules, and exercise the marked quickstart without network or extras.

- Add a generated, offline API reference covering supported root and explicit extension exports.

- Ship maintained usage and architecture docs plus examples with the wheel. Add
  `linch docs path` to locate them, including a source-checkout fallback for editable installs.

## 3.0.1 — 2026-11-02

This patch release adds `Agent.reload_skills()`, an owner-approved
exception to the MINOR rule in `docs/versioning.md`.

- Freeze each ordinary session's system blocks and ordered tool schemas in durable
  metadata so stable catalogs and worker restarts preserve the provider prefix.
  Skill listings move into the cacheable system prefix and are deduplicated in
  requests that bypass snapshots.
- Add async `Agent.reload_skills()` and preserve registry order when reloading
  subagents. Catalog changes arrive as durable conversation reminders; tool
  additions append schemas, schema edits retain their slot, and removed tools keep
  their wire schema while execution reports the existing unknown-tool error.
- Preserve system and tool bytes through compaction and consolidate accumulated
  catalog changes into one subsequent reminder. Persist separate base and
  notified digests, including skills, for restart deduplication and crash recovery.
  Description changes carry the new description; MCP notifications use names only.
- Add off-by-default `native_catalog_changes` to Anthropic and OpenAI Responses
  provider options. Eligible models carry additions in durable positional tool
  blocks while retaining the tool prefix; narrowing and schema edits use the
  phase-2 path, and append-only fallback includes native additions. Anthropic
  by-value removals remain text-only. Both native paths are **UNVERIFIED against
  live endpoints**; fake-transport tests cover wire payloads and durable replay.

## 3.0.0 — 2026-11-01

### Breaking and new features

- **Kernel and plugins.** `Context`, reversible `Disposable`, typed lifecycle bus, and `Plugin` mounts through `agent.use()` are now the extension foundation; see [the kernel guide](docs/architecture/kernel.md#plugins).
- **Versioned profiles.** `Profile` and `RunOptions(profile=...)` select immutable per-run roles, plugin rows, tool restrictions, budgets, schemas, and resume-pinned content; see [domain profiles](docs/usage/profiles.md).
- **Deferred tool search.** `ToolSearchPlugin` keeps small catalogs byte-identical and defers large tool/skill schemas until `tool_search` loads them; see [tool search](docs/usage/tools.md#tool-search-for-large-catalogs).
- **Lifecycle catalog and tracing.** Typed lifecycle points have a generated producer/consumer catalog, and `trace_id` follows events, logs, tools, jobs, and subagents; see [events](docs/architecture/events.md#typed-lifecycle-catalog).
- **Per-run identity and RBAC.** `Principal(tenant, user, roles)` is carried through `RunOptions`, permission rules, tool context, and the durable run contract; see [role rules](docs/usage/tools.md#per-run-principals-and-role-rules).
- **RAG and grounding.** `RagConnector` returns sourced chunks and optional final-answer grounding enforces `[doc:<id>]` citations; see [context and memory](docs/usage/context-and-memory.md#sourced-retrieval-and-grounding).
- **Durable run ownership.** Postgres runs use lease epochs, fenced writes, checkpoints before side effects, and reconnect-by-sequence event streams; see [run stores](docs/usage/run-stores.md).
- **Worker fleet runtime.** Optional `linch.worker` uses PgQueuer for dispatch, leases, retries, graceful drain, cron, and takeover after worker/database failures; see [production scale-out](docs/usage/production.md#scale-out).
- **File-backed memory.** Scoped `MEMORY.md` indexes are loaded through the filesystem backend; semantic recall is an optional host plugin rather than a core memory store; see [file memory](docs/usage/context-and-memory.md#file-memory).
- **OpenTelemetry instrumentation.** Core uses `opentelemetry-api` at run/turn/provider/tool/compaction boundaries; hosts own SDK/exporter setup and message-content capture policy; see [traces and metrics](docs/usage/events.md#opentelemetry-traces-and-metrics).
- **Explicit extension namespace.** Workflows, evals, deep-agent presets, reports, loop runners, and the scaffold CLI live under `linch.ext.*`; see [the extension boundary](docs/architecture/overview.md#extensions).
- **Neutral agent defaults and tool protocols.** Bare agents have no domain tools or discovery features by default; `workspace_tools()`, duck-typed tools, reversible registry handles, hooks, and explicit plugin mounts make trust boundaries opt-in; see [tools](docs/usage/tools.md).
- **Reliability and finalization.** Default response reliability, prerequisites, completion gates, and the serial `turn-stopping` seam make malformed or incomplete responses observable and recoverable; see [reliability](docs/usage/agent.md#tool-and-response-reliability).
- **Unified execution world.** Shell and filesystem access use coherent `ExecutionBackend` transports with durable policy identity; see [execution backends](docs/usage/extending.md#executionbackend--one-shell--filesystem-world).
- **Append-only session state.** `SessionLog` is the durable source for detached `provider_view`/`full_history` projections; per-request RAG/context remains ephemeral; see [session invariants](docs/architecture/invariants.md).
- **Canonical tool output and durable delivery.** JSON Schema tool results, progress/attachments, durable inbox notifications, and exact pending model-input snapshots are available through the Postgres capabilities; see [events](docs/usage/events.md).
- **Accounting and compaction.** Model-call receipts settle cumulative primary/compaction usage before budget admission, with fail-closed unknown usage and durable built-in compaction replay; see [accounting and compaction migration](docs/migration-3.0.md#accounting-and-compaction).
- **Wire and runtime floor.** Python 3.11+ and wire generation 3 are required; pre-3.0 persisted data and legacy readers are rejected; see [Migrating to 3.0](docs/migration-3.0.md).
- **Provider and dependency changes.** Gemini/Vertex AI use `google-genai`, durable state is Postgres plus InMemory for tests/dev, and SQLite stores are removed; see [provider setup](docs/usage/providers.md) and [run stores](docs/usage/run-stores.md).

See [Migrating to 3.0](docs/migration-3.0.md) for replacement imports,
removed compatibility paths, and required durable-state changes.

### Fixed

Found by the 3.0 release reviews. 3.0 fixes them all; there is no 2.x backport.

#### Present in 2.0

- **Security.** Grep no longer lets the model pass ripgrep flags through
  `pattern`; the Glob/Grep fallbacks without ripgrep no longer escape `cwd`;
  a windowed Read no longer grants Edit on unseen regions; isolated subagents
  run filesystem tools in the isolation directory, not the parent workspace.
- **Files.** Edit/Write are atomic and keep line endings; `DiskFileBackend`
  keeps CRLF/CR; `PostgresFileBackend.edit` no longer loses concurrent updates
  and `ls` escapes `_`/`%` in prefixes; isolation cleanup covers copy failure,
  cancellation and outside symlinks.
- **Loop and resume.** Resume no longer turns a final-tool ack, hook retry or
  truncation nudge into a failed run; closing a run closes nested provider
  streams, tool batches and limiter slots; skill tool restrictions survive
  resume; the loop-guard force-final turn cannot execute tools; the recovery
  hint follows the tool result; a failed terminal write no longer records two
  results.
- **Subagents and mailbox.** Continued workers charge the current run's budget;
  mailboxes are addressed by unique worker id and ambiguous display names are
  rejected; subagents inherit the run's `deps`; a background subagent that
  fails before starting reports its failure; `align(timeout_s=...)` never
  applies a message it reported as timed out; notifications and mailbox
  messages are not lost when a run closes mid-delivery.
- **Providers.** Anthropic tool calls with no arguments work; Gemini
  `MALFORMED_FUNCTION_CALL` is an error, image attachments are sent, identical
  parallel calls keep separate ids, and server retry delays are honored (a
  delay above `max_delay_ms` fails instead of being shortened).
- **Other.** MCP attachment is all-or-nothing and closes the connection on
  failure; workflow replay keys no longer embed object reprs; `run_workflow`
  closes its host session on setup failure; the Redis mailbox recipe keeps
  `type`/`in_reply_to`; the mem0 recipe adds only the new run; cyclic or
  deeply nested tool metadata no longer crashes persistence.

#### Introduced during 3.0 development

- **Profiles.** A run `Profile` is now a per-run overlay: profile plugins,
  tools, permissions and prompts no longer leak to the Agent or other runs,
  forked children keep working, and breaking out of a stream on the final
  `ResultEvent` no longer leaves the session busy.
- **Durable runs and workers.** Run-store writes carry an explicit lease, so a
  worker that lost its lease can no longer write; backend parity fixes for
  leases, checkpoints and snapshots; snapshot pruning waits for the committing
  checkpoint; the shared Postgres pool stays open across worker teardown;
  duplicate-note and outage harness checks are real.
- **Identity, retrieval and telemetry.** RAG retrieval uses the run's
  `Principal`; telemetry captures prompt and tool content only after
  redaction, keeps the no-SDK fast path, follows service changes, and leaves
  recovered provider steps out of ERROR.
- **Accounting.** `Usage.input_tokens` excludes cache read/creation tokens on
  every provider; auxiliary calls disable thinking only where the provider
  allows it; compaction recovery keeps integrity after crashes.
- **Events.** `IgnorableEvent` validates with the codec's limits; attachments
  deeper than the limit are dropped on every Python version.

### Development entries

- Interrupt resumable runs on transient durable-store connection failures without
  writing a failed terminal; worker retry or lease takeover resumes the checkpoint.

- Migrate Gemini and Vertex AI to `google-genai`, preserving streaming tools and structured output, with native thinking, signed-part replay, token accounting, typed errors and async transport cleanup.

- Reconnect workers after transient database outages with bounded jitter, a fresh
  queue/LISTEN connection per manager cycle, and cancellation-safe callback drain.

- Create the initial startup checkpoint atomically with run adoption, recover
  startup events and append the prompt once across worker takeover, and commit
  worker setup failures with an atomic error/result tail and journal totals.

- Add default, provider-independent response reliability with a shared bounded
  correction budget, optional tool prerequisites and completion gates, durable
  run-local state, and `ReliabilityEvent`. `reliability=None` opts out. Text
  tool-call envelopes request a native resend; text rescue is dropped.

- Reuse worker-owned run and session stores across jobs, verifying schemas once
  per borrowed pool. Factories must use both `job.run_store` and
  `job.session_store`; agent teardown preserves their initialized handles.

- Commit terminal status and pending events atomically, preserving guard-tail
  replay across crashes and cancellation.

- Bound checkpoint recovery state with journal watermarks and fixed-size
  BLAKE2b loop-guard digests; remove the private guard-delta journal while
  retaining detached projection getters and the incremental history digest.
- Require combined checkpoint/event writes when a phase has pending events,
  preserving durable-before-side-effect ordering. Postgres writes now use
  fenced autocommit statements with last-sequence allocation, and store-owned
  pools use a clean asyncpg reset callback. The hot-path loop also avoids
  duplicate checkpoint serialization traversals. A D15 msgspec checkpoint
  prototype measured below 1% on the scale cases and was dropped. See the
  [hot-path measurements](docs/superpowers/measurements/2026-10-31-hot-path.md).

- Add `RunOptions.run_id` to start or adopt a pending run through the ordinary session API.

- Retry nonterminal worker exits after ownership loss or cancellation. Retry
  transient lease renewal failures within the last confirmed TTL; cancel the
  run task on lease loss/expiry without writing a terminal aborted status.
  Protect teardown from queue callback cancellation. Move pending-run inserts
  and contract-checked adoption into the run stores with shared conformance tests.

- Add explicit `linch.worker` (`worker` extra): atomic pending-run dispatch and
  batching through PgQueuer, fleet queue limits, lease heartbeats, checkpoint
  recovery, W3C trace propagation, and deferred cron/interval schedule jobs.
  Replace coordination scheduling and `Agent(schedule_store=...)` with
  host-mounted, reversible schedule tools. Add a runnable fleet example and
  local enqueue/NOTIFY measurements to the production guide.

- Raise public `RunNotResumableError` with `run_id` and `status` when resuming
  terminal runs or runs without checkpoints, replacing the silent no-op.
- Remove all SQLite stores and their executor. Durable sessions, runs, and
  virtual files use Postgres; InMemory stores remain for tests and development.
  Built-in mailboxes now last only for the process lifetime.
- Require explicit session/run stores and an explicit enabled filesystem for
  `create_deep_agent(durable=True)`; use `durable=False` for an ephemeral preset.
- Read durable run reports from Postgres via `scripts/run_report.py --dsn` or
  `LINCH_POSTGRES_DSN`; remove `--runs-db`. Runtime benchmarks use memory stores
  and no longer apply former database latency thresholds.
- Replace the custom observer protocol, dispatchers, collectors and
  `RunTelemetryHook` with direct OpenTelemetry source instrumentation. The API
  is a core dependency; the SDK/exporter is host-owned and test/dev-only here.
  Inject providers through Agent Context for tenant isolation. Emit run, step,
  chat, tool and compaction spans plus token, operation and store histograms;
  message/tool content requires the standard OTel capture environment switch.
  No-op spans pass streamed iterators through unchanged; metrics-only hosts and
  valid sampled-out trace contexts retain instrumentation.

- Require Python 3.11+; remove the Python 3.10 scaffold TOML fallbacks.
- Require shared wire version 3 for all persisted envelopes, including workflow
  metadata. Reject missing, old, or unknown versions; start new runs and use
  fresh databases. Remove automatic schema upgrades, legacy resume overrides,
  checkpoint defaults, cursorless tool recovery, and old workflow readers.
- Require coherent `.shell`/`.fs` execution backends; remove the shell-only
  adapter and unaccounted stream entry point.
- Always validate configured structured-output schemas with core `jsonschema`;
  remove the missing-dependency bypass.
- Use one compaction recovery driver, preserving default per-turn retry and
  ladder per-run limits, accounting, and lifecycle-hook ordering.
- Remove legacy public API aliases, unused configuration/middleware types,
  and camelCase properties/arguments. Use the configurable `workspace_tools`
  preset and `ToolRegistry.register/unregister`.
- Remove dormant discovery types/codecs/capability plumbing and obsolete
  runtime helpers; reject removed discovery controls during message decoding.
- Consolidate strict JSON traversal, custom token estimation, Postgres pool
  lifecycle, permission signatures, client close, and claim validation while
  preserving serialization bytes, fingerprints, and caller-specific policies.
- Remove stale Studio ignore/documentation references.
- Replace keyword, tiered, and database memory stores with run-scoped file
  memory: `ScopedFileBackend` isolates paths, `FileMemoryContextBuilder` loads
  `/memories/MEMORY.md` once per run (bounded to 200 lines), and the mem0
  integration is documented as an optional example recipe.
- Replace the old versioning exceptions with the unrestricted 3.0 transition
  policy and normal SemVer for releases after 3.0 stable.

See [Migrating to Linch 3.0](docs/migration-3.0.md) for each removal,
and replacement.

## 3.0.0a1 — 2026-10-30

This is the first incremental 3.0 prerelease. It adds model-call accounting
for the primary turn and context compaction while keeping the existing neutral
defaults, persona, prompts, and opt-in compaction strategies unchanged. The
later 3.0 reliability policy and other roadmap lanes are not part of this
release.

### Added

- **Cumulative model-call usage.** Reported usage is settled once per physical
  provider delivery and the run total now includes both primary deliveries and
  auxiliary compaction deliveries. A rejected or unusable compaction response
  still counts when the provider reached `message_end` with valid usage.
- **Accounting journal surface.** `ModelCallEvent` and
  `is_model_call_event` are public for typed journal envelopes. Ordinary
  primary model-call records remain journal-only; auxiliary compaction usage is
  represented by additive `UsageEvent` metadata (`source="compaction"`,
  `call_id`, `logical_call_id`, and `model`).
- **Budget-safe compaction.** A compaction receipt settles before hooks,
  summary application, or the next dispatch. Admission is checked again after
  compaction, so a summary that exhausts a token or USD cap prevents the main
  turn from starting.
- **Fail-closed unknown usage.** A dispatched delivery without valid final
  usage is unknown, not zero. Capped runs stop before another model call until
  the host resolves the accounting fence; uncapped runs may finish with a
  lower-bound total and `usage_complete=False`.
- **Durable built-in recovery.** With matching durable run/session state,
  accepted `DefaultCompaction` and `DetailedCompaction` receipts carry a
  bounded summary and ordered hash-only source anchor. Resume can apply that
  paid result without another provider call or duplicate charge. A custom
  strategy using Linch's supplied provider facade receives accounting,
  cancellation, and limiter guarantees, but arbitrary external calls or
  transformations do not gain deterministic paid replay.

### Migration notes

- No prompt, persona, default-strategy, or normal primary-input behavior
  changes in this prerelease. Existing custom strategies remain supported when
  they use the supplied provider facade for calls that should be accounted.
- Unknown or unpriced models do not receive a synthetic USD price. Token usage
  remains known when reported, while aggregate cost is partial and cost caps
  cannot enforce an unknown price.
- Receipt ownership is physical delivery ID, not logical operation ID. Retries
  and fallbacks that reach final usage are separate billed deliveries.
- Durable runs with an old in-flight accounting shape fail closed, including
  when legacy resume is explicitly opted into; start a fresh run or migrate the
  durable state under the matching contract.
- This release does not promise distributed-tree cap recovery, exactly-once
  external-provider billing, or reservations against concurrent admissions.

## 2.0.0 — 2026-09-14

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
  `IgnorableEvent` and skipped. `ignorable` is reserved for unknown types: a
  *known* event type carrying the flag is now rejected instead of decoding as
  itself. An unrecognized `prompt_cache_advisory` `reason` is likewise rejected
  rather than silently coerced to `"tool_set_changed"`; a row that omits the
  field still defaults, so legacy rows keep loading.

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
  Postgres storage allocation. Background worker audit events carry their
  origin, and the opt-in durability ledger adds durable delivery after enqueue
  — not recovery of a worker that was still running when its process stopped,
  which remains the embedding application's responsibility.


### Fixed

- `DockerBackend` now declares `confinement`, so an agent configured with it
  again reports Bash as sandboxed in the system prompt. The tri-state
  sandbox reporting introduced with the execution seam requires an explicit
  confinement declaration, which Linch's own Docker backend did not make —
  it was described to the model as unverified. Durable resume identity is
  unchanged: a shell-only backend still fingerprints through its
  `resume_policy_config`, which does not include confinement.
- `LocalExecutionBackend` now declares `security_posture = "host"`. Passing it
  explicitly described Bash to the model as an undeclared boundary, when the
  world in fact declares it runs on the host; "unverified" is now reserved for
  a backend whose boundary Linch genuinely cannot describe.
- A tool call's `ctx.execution` can no longer be swapped by a pipeline listener
  after permission. Filesystem tools fall back to `execution.fs` and shell
  access rides the same world, so swapping it redirected an approved call into
  another workspace.
- `RemoteExecutionBackend` rejects a `resume_policy_config` that defines
  `confinement`; the seam merges its own declaration under that key, which
  silently dropped the caller's value from the resume fingerprint.
- A `ConfigError` about an execution world inherited from a supplied `Context`
  now says so, instead of blaming an `execution_backend=` argument the caller
  never passed.

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
  [docs/versioning.md](docs/versioning.md#dependencies-and-reproducible-builds),
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
