# Migrating to Linch 3.0

This guide tracks the ongoing 3.0 transition, including the original
`3.0.0a1` Release-A accounting slice and subsequent API cleanup. Python 3.11+
is required. During 3.0 development, compatibility breaks do not require a
warning window or compatibility shim.

All persisted envelopes share wire generation **3**, including checkpoints,
run contracts, model-call intents/receipts and journals, exact model-input
snapshots, permission decisions, session/provider-view snapshots, events, and
workflow run metadata. Missing, pre-3.0, or unknown versions are rejected;
start new runs and use fresh databases. Built-in stores do not upgrade old
schemas. `linch.RUN_SCHEMA_VERSION` reflects the shared private wire version.

## API and internal cleanup

- **WP7 reliability:** `Agent` now defaults to `ReliabilityPolicy()` for bounded
  invalid-response corrections; pass `reliability=None` to opt out. Optional
  `ToolPrerequisite` and `CompletionGate` rules use successful foreground calls
  in the current run. Text `<tool_call>` rescue is dropped: whole-text envelopes
  request a native resend and never execute tools. Reliability policy is part
  of the resume contract; changed policies require a new run.

Each item below records a removal or consolidation in the legacy sweep.
Private helper names are listed for maintainers; supported application imports
remain the names in `linch.__all__`.

- **Python / K16:** Python 3.10 support and the CLI scaffold's `tomllib` import/line-scan fallbacks are removed; use Python 3.11+ and valid TOML.
- **A01:** `Session(provider_view=..., full_history=...)` is removed; pass `session_log=SessionLog.seed(historical=..., visible=...)`.
- **A02:** `SessionLog.derive_messages()` is removed; read `provider_view`.
- **A03:** Shell-only `Agent(execution_backend=...)` values are removed; supply one coherent `.shell`/`.fs` world with `LocalExecutionBackend` or `RemoteExecutionBackend`.
- **A04:** `BashTool(backend=...)` is removed; use `BashTool(execution=...)` with a coherent execution backend.
- **A05:** Agent constructor aliases `systemPrompt`, `maxRetries`, `maxOutputTokens`, `includePartialMessages`, `maxTurns`, `maxToolConcurrency`, `toolBatchingStrategy`, `toolTimeoutMs`, `cacheTtl`, `configDir`, `mcpServers`, and `loopGuard` are removed; use their snake_case spellings.
- **A06:** The `Agent.openai` provider property is removed; use `agent.provider`; the separate `Agent(openai=...)` configuration argument remains supported.
- **A07:** The unused `AgentOptions` configuration DTO is removed; configure `Agent(...)` directly.
- **A08:** `coding_tools`, `default_tools`, `defaultTools`, and `tools_from_defaults` are removed; use `workspace_tools(exclude=..., extra=...)` for the configurable workspace preset.
- **A09:** `ToolRegistry.add/remove` are removed; use `register/unregister`; `register` and `replace` still return reversible `Disposable` handles.
- **A10:** Tool-context aliases `sessionId`, `runId`, `sessionStore`, `fileReadTracker`, `idempotencyKey`, `reportProgress`, and middleware aliases `sessionId`, `runId`, `turnIndex`, `toolUseId`, `toolName` are removed; use snake_case attributes and `report_progress`.
- **A11:** The unused `AgentMiddleware` base is removed; implement the duck-typed hooks consumed by `ToolMiddlewareHook`.
- **D01:** Obsolete middleware outcome/dispatch helpers are removed; the active `ToolMiddlewareHook` adapter remains.
- **D02:** Background-tool completion no longer appends directly to a duck session's notification list; custom session implementations must supply async `notify(...)`.
- **D03:** The `parallel_safe` fallback is removed; read tools must declare `parallel` to opt into concurrent scheduling.
- **D04:** The dormant runtime-control codec capability gate and store capability attributes are removed with deferred discovery support.
- **P01:** Checkpoint field defaults on read are removed; every serialized current checkpoint field is required, including empty extension state and nullable fields.
- **P02:** Default-version and missing-fingerprint run-contract readers are removed; current contracts require a verified fingerprint.
- **P03:** `RunOptions.allow_legacy_resume` and contractless resume are removed; start a new run with a current execution contract.
- **Resume:** Terminal runs and runs without checkpoints raise `RunNotResumableError` with `run_id` and `status`; start a new run.
- **P04:** Cursorless assistant-boundary tool recovery is removed; tool recovery requires a valid `tool_batch_event_after_seq` cursor.
- **WP8:** Runtime checkpoints use private model-call journal watermarks and bounded loop-guard digests; old receipt snapshots and pre-3.0 guard shapes are rejected, so start a new run.
- **P07:** Pre-3.0 permission readers and the null `updated_input` wire marker are removed; non-null persisted input rewrites and live approval rewrites remain rejected.
- **P09:** Automatic SQLite/Postgres schema upgrades are removed; built-in database stores create complete version-3 schemas and reject old, missing, or unknown schema metadata on existing databases.
- **WP8 Postgres:** Fresh run/session schemas require their durable event/message sequence counters; old shapes are rejected before DDL. `ModelInputSnapshotStore.delete` now requires `run_id` so cleanup can be fenced in one statement.
- **P10:** Legacy scalar/malformed failure-history preservation is removed; failure writes append only to a valid current list of error objects, replacing malformed history.
- **Workflow journals:** Old call-key algorithm readers and `fingerprint_version` branches are removed; durable workflow metadata requires `schema_version=3`, and incompatible runs must start anew.
- **P08:** `ToolActivation`, `ToolSurfaceReaderGuard`, `tool_activations`, and their discovery codecs/projection plumbing are removed; unknown message-block types and unknown tool-result fields fail closed, while ordinary message-copy isolation remains intact.
- **D05:** The unused runtime `PermissionDecision.updated_input` field is removed; persisted non-null input rewrites remain rejected (the null marker was subsequently removed by P07).
- **D06:** The unused private `add_usage` helper is removed; use `Usage.add`.
- **D07:** The unused private `make_pool` factory is removed; Postgres stores own their pool configuration through the shared lifecycle helper.
- **D08:** The unaccounted `stream_turn` entry point is removed; parser tests use `_stream_turn`, while loop deliveries use the required-ledger accounted path.
- **D09:** The obsolete `glob.glob` TypeError retry is removed; supported Python versions accept `root_dir` and `recursive` directly.
- **D12:** The `linch.memory` package and its `MemoryStore` contract are removed; use scoped file memory through `FileMemoryContextBuilder` and the existing `FileBackend` tools.
- **D12:** `ConsolidationGate` is removed; application-owned file-memory policy can update or delete stale fact files through the backend.
- **D12:** `InMemoryKeywordMemoryStore` is removed; use `StateFileBackend` for test memory files.
- **D12:** `MemoryContextBuilder` is removed; use `FileMemoryContextBuilder` with `ContextInjectionHook`.
- **D12:** `MemoryExtractionContext` is removed; terminal-turn extraction is application policy or an optional recipe.
- **D12:** `MemoryExtractor` is removed; application hooks may write facts through the existing file tools.
- **D12:** `MemoryItem` is removed; store one fact per filesystem file.
- **D12:** `MemorySearchResult` is removed; semantic search result types belong to the host or optional plugin.
- **D12:** `MemorySearchTool` is removed; the model uses the existing filesystem tools for memory files.
- **D12:** `MemoryUpsertTool` is removed; the model uses the existing filesystem tools for memory files.
- **D12:** `PostgresMemoryStore` is removed; configure the production `FileBackend` (typically Postgres-backed) in run dependencies.
- **D12:** `SqliteMemoryStore` is removed; SQLite memory persistence is not supported in 3.0.
- **D12:** `TieredMemoryStore` is removed; represent durable facts as files and apply host-specific policy around them.
- **D12:** `format_memory_context` is removed with the memory package.
- **D12:** `latest_user_text` is removed with the memory package.
- **D12:** `resolve_memory_store` is removed with the memory package.
- **D12:** `MemoryExtractionHook` is removed; use `ContextInjectionHook(FileMemoryContextBuilder())` or an application-owned hook.
- **D12:** `assert_memory_store_contract` is removed from `linch.testing`; use the file-backend contract helper.
- **D12:** The `memory_recalled` eval scorer is removed; score host-owned semantic recall or inspect file-memory events in application evals.
- **D12:** Legacy `RunReport.long_run` memory-search, recalled-id, and tier-count metrics are removed; report host-owned file-memory or semantic-recall signals instead.
- **D10:** The missing-`jsonschema` validation bypass is removed; `jsonschema` is a core dependency and structured output is always validated against its configured schema.
- **M01:** Duplicate compaction retry drivers are consolidated into `_stream_turn_with_recovery`, preserving one retry per turn without a ladder and the ladder's per-run forced-compaction limit, ordering, and accounting.
- **M02:** Duplicate strict JSON traversals share a private traversal core with separate boundary adapters; serialized bytes, fingerprints, depth/error policies, and permissive metadata conversion are preserved.
- **M03:** Compaction, context building, and offload share custom-estimator invocation; each retains its message/text counting and fallback policy.
- **M04:** Loop-guard signatures use `permission_decision_key` instead of a duplicate canonicalizer; canonical JSON bytes and ASCII escaping are unchanged.
- **M05:** Four Postgres stores share pool initialization/close helpers; locking, schema transactions, retry, supplied-pool handling, and run-store advisory-lock/reset policies remain unchanged.
- **M06:** OpenAI Chat uses the existing async client-close helper; client fields still reset before closing and close errors propagate.
- **M07:** Mailbox and scheduling claims share a neutral validator; validation order and error messages are unchanged.
- **Studio leftovers:** The `linch_ui/` ignore rule and stale Studio branch/DeepSeek documentation references are removed.

P05 accounting recovery safeguards remain: aggregate usage is not a substitute
for valid delivery receipts. P06 event readers remain strict: unknown required
events fail, and only valid unknown envelopes explicitly marked `ignorable: true`
round-trip as `IgnorableEvent`.

## Accounting introduced in 3.0.0a1

The following sections preserve the accounting contract introduced by the
first prerelease. That slice kept the neutral Agent persona/prompts and the
existing compaction strategies; it did not itself introduce later reliability
or distributed accounting features.

## Pin the prerelease deliberately

Pin this prerelease while reviewing the new accounting fields and durable-run
semantics:

```toml
dependencies = ["linch==3.0.0a1"]
```

Keep the optional extras needed by your application and resolve their declared
dependency ranges in your lockfile. The accounting feature itself does not
require an unrelated dependency upgrade.

## Accounted usage is cumulative

Every physical provider delivery that reaches normalized `message_end` with
valid `Usage` is settled once by delivery ID. The run total includes:

- primary model deliveries;
- built-in compaction deliveries; and
- calls made by a custom compaction strategy through Linch's supplied provider
  facade.

Retries and fallbacks receive new delivery IDs and count separately when they
reach final usage, even if they reuse one logical operation. A compaction
response can be rejected because it is empty, non-text, tool-using, oversized,
or otherwise unusable; valid final usage is still charged.

The existing `UsageEvent` remains compatible. Auxiliary compaction usage adds
`source="compaction"`, `call_id`, `logical_call_id`, and `model`. The public
`ModelCallEvent` / `is_model_call_event` pair exposes the typed journal envelope
for store and journal observers; ordinary primary delivery records remain
journal-only and are not extra normal stream events.

`ResultEvent.total_usage` and run reports expose the cumulative known total. If
a dispatched delivery is cancelled, fails, or ends before valid final usage,
the usage is unknown rather than zero and the terminal result sets
`usage_complete=False`. An uncapped run may finish with that lower bound.

## Budget behavior changes at the compaction boundary

Compaction is charged against the same run budget as the primary turn. Receipt
settlement happens before summary application, hooks continue, or another
provider dispatch. The loop rechecks admission after compaction, so a summary
that exhausts a token or USD cap prevents the main turn from starting.

For a capped run, unknown usage is a strict fence: Linch stops before another
model call rather than claiming that the configured cap is enforced. A
pre-dispatch cancellation or admission denial is not billed; a dispatched call
without valid final usage is not silently treated as free.

The same live `RunBudget` object shares receipt ownership and charged delivery
IDs across the in-process agent/subagent tree. A resume may adopt a checkpoint
with a fresh zero-spent budget; a fresh nonzero or owner-mismatched capped
budget fails closed rather than replaying charges. These are in-process
guarantees only: distributed-tree recovery, exactly-once external-provider
billing, and reservations against concurrent admissions are not promised.

## Durable compaction replay

`DefaultCompaction` and `DetailedCompaction` retain their existing strategy
semantics and prompts. In a durable run, an accepted summary carries a bounded
receipt-backed result and an ordered hash-only source recipe. Required invoked
skill bodies are flushed through the existing session store before the recipe
is written. On resume, a matching source, strategy configuration, run
contract, and canonical skill-listing wrapper allow Linch to apply the stored
summary without another provider call or duplicate charge.

Linch fails closed when the source order, durable history boundary, receipt,
skill body, listing wrapper, or contract does not match. A provider-view cache
is verification material, not source authority. Old in-flight accounting state
also fails closed; start a new run rather than asking Linch to guess whether an
interrupted delivery was billed.

## Custom and standalone compaction

The public `CompactionStrategy` protocol is unchanged. A custom strategy that
uses the provider facade receives accounting, run cancellation, and one
provider-slot/limiter acquisition per physical stream. Opaque network calls
outside that facade are not observable by the SDK. Arbitrary multi-call or
transform behavior has no automatic deterministic paid-result replay guarantee;
provide an explicit deterministic result/commit seam if the host needs one.

The standalone `summarize_with_provider(...)` helper remains available with
its existing direct-provider behavior. Calling it outside the normal run does
not create a durable run receipt or add usage to a `RunBudget` unless the host
integrates that accounting itself.

## Pricing and compatibility limits

Unknown or unpriced models never receive a synthetic USD price. Their reported
tokens remain in cumulative usage, while cost fields and known-cost aggregates
are partial (`None` where no price is known). A USD cap cannot enforce an
unknown price; use a custom pricing table when the host knows the model's
rates.

The original accounting slice did not itself add reliability correction
policy, bounded feedback, or default-on reliability controls. Its accounting
contract does not promise distributed accounting, exact external billing, or a
reservation protocol. Later 3.0 changes are tracked separately above and in
the changelog.

See [Events](usage/events.md), [Agent & session](usage/agent.md), and
[Compaction architecture](architecture/compaction.md) for the operational
details.

## Postgres-only durable state (WP17 / D13)

- `SqliteRunStore` is removed; use `PostgresRunStore(dsn)` for durable runs or `InMemoryRunStore` for process-lifetime tests/dev.
- `SqliteSessionStore` is removed; use `PostgresSessionStore(dsn)` for durable history or `InMemorySessionStore` for process-lifetime tests/dev.
- `SqliteFileBackend` is removed; use `PostgresFileBackend(dsn)` for durable virtual files or `StateFileBackend` for ephemeral state.
- `SqliteMailbox` is removed; use `InMemoryMailbox` for process-lifetime messaging or a host-owned `Mailbox` adapter for external infrastructure.
- `create_deep_agent(durable=True)` requires explicit `session_store`, `run_store`, and an explicit `filesystem` when enabled; supply Postgres adapters for restart durability or use `durable=False` for an ephemeral preset. Explicit InMemory overrides remain accepted.
- `scripts/run_report.py --runs-db` is removed; pass `--dsn` or set `LINCH_POSTGRES_DSN`. `load_report` and `render_report` now take a Postgres DSN string.

The SDK does not read DSNs from the environment automatically. Hosts read their
configuration and construct the stores explicitly. Install `linch[postgres]`
and follow the [local Docker Postgres setup](./usage/quickstart.md#5-keep-state-when-the-process-restarts).
Existing incompatible persisted data is rejected; start fresh runs rather than
attempting a legacy database upgrade.

## Coordination scope

- `Schedule` is removed; define schedules through `linch.worker`.
- `ScheduleOccurrence` is removed; worker schedule fires use queue payloads.
- `ScheduleStore` is removed; use the worker Postgres schedule-definition table.
- `ClaimingScheduleStore` is removed; PgQueuer claims deferred jobs.
- `LeasedScheduleStore` is removed; worker queue delivery handles retries.
- `InMemoryScheduleStore` is removed; use an explicit worker/Postgres adapter.
- `SchedulerLoop` is removed; PgQueuer dispatches runs and deferred schedule jobs.
- `schedule_tools` is removed; use `linch.worker.register_schedule_tools` (returns a `Disposable`).
- `render_schedule_message` is removed; a schedule fire dispatches a separate run.
- `cron_matches` is removed; worker cron definitions use `croniter`.
- `next_cron_time` is removed; worker cron definitions use `croniter`.
- `validate_cron` is removed; worker cron definitions use `croniter` validation.
- `assert_schedule_store_contract` is removed with the scheduling protocol.
- `Agent(schedule_store=...)` is removed; hosts register worker tools explicitly.
- Worker factories must reuse `job.session_store` as well as `job.run_store`; both adapters live for the worker lifetime.

`InMemoryMailbox` remains available for process-lifetime messaging; durable
mailbox adapters remain host-owned. See [Scale out](usage/production.md#scale-out)
for worker topology and schedules.

## Direct OpenTelemetry (WP15 / D11)

- **D11:** `RunObserver` is removed; configure an OpenTelemetry API tracer/meter through Agent Context.
- **D11:** `BaseObserver` is removed; use native hooks for application behavior and OTel for telemetry.
- **D11:** `ObserverDispatcher` is removed; OTel SDK span processors/exporters provide telemetry fan-out.
- **D11:** `LoggingObserver` is removed; configure an OTel ConsoleSpanExporter.
- **D11:** `SpanCollector` is removed; use the SDK InMemorySpanExporter in tests.
- **D11:** `OpenTelemetryObserver` is removed; Linch emits OTel spans directly; hosts configure SDK providers.
- **D11:** `RunTelemetryHook` is removed; remove the hook; OTel source instrumentation is always present.
- **D11:** `RunInfo` is removed; use invoke_agent span attributes or native AgentStartContext.
- **D11:** `RunResultInfo` is removed; use invoke_agent span attributes or native AgentStopContext.result.
- **D11:** `TurnInfo` is removed; use step spans or native turn hook contexts.
- **D11:** `ProviderCallInfo` is removed; use chat span attributes or native provider hook contexts.
- **D11:** `ProviderCallResult` is removed; use chat usage/finish attributes or native provider hook contexts.
- **D11:** `ToolInfo` is removed; use execute_tool span attributes or native tool hook contexts.
- **D11:** `ToolResultInfo` is removed; use execute_tool span attributes or native tool hook contexts.
- **D11:** `Span` is removed; use the SDK ReadableSpan type.
- **D11:** `normalize_observers` is removed; configure SDK processors/exporters directly.
- **D11:** `observers=` is removed; pass OTel services through `Agent(context=...)`.
- **D11:** `linch.observability` is removed; use `opentelemetry` directly.
- **D11:** The `[otel]` extra is removed; install `opentelemetry-sdk` and your exporter in the host application.
- **D11:** `examples/observability/custom_observer.py` is removed; use the console/OTLP exporter example.

## Pending run adoption

- **WP8:** Loop-guard checkpoint call-count keys are now 64-bit BLAKE2b digests of the permission decision key; pre-3.0 guard checkpoints are rejected.

- `RunStore.create_run(session_id, id=existing_id)` adopts only a same-session `pending` run, merges metadata and checks existing contract compatibility; other existing IDs raise `ValueError`. Queue submission remains idempotent through `enqueue_run`.

- Custom run stores must accept `mark_failed(..., *, events=())` and return `(run_record, event_seqs)` so failed terminals commit with their pending events.
