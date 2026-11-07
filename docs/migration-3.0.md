# Migrating from Linch 2.x to 3.0

Read this page when upgrading a Linch 2.x application to Linch 3.0.

Linch 3.0 is a headless agent kernel. Python 3.11+ is required, and so is
`linch-native`, a compiled extension that `pip install linch` pulls in;
building it from source needs a Rust toolchain. There are no
compatibility shims or readers for pre-3.0 persisted data. Start new runs and
use fresh Postgres schemas; built-in stores reject incompatible existing
schemas rather than upgrading them.

Kernel exports are listed in `linch.__all__`. Extensions are imported
explicitly from `linch.ext.*` and are absent from the kernel's top-level API.
The tables below list the application-facing changes by area.

## Agent, sessions, and tools

| Removed or changed in 3.0 | Replacement |
|---|---|
| Python 3.10 | Python 3.11+ |
| `AgentOptions` | Configure `Agent(...)`; `from linch import Agent` |
| Domain configuration selected only at Agent construction | `Profile` layers selected with `RunOptions(profile=...)`; pass the pinned id/version/definition again on resume or takeover; see [profiles](usage/profiles.md) |
| `Agent.openai` provider property | `agent.provider`; the separate `Agent(openai=...)` configuration argument remains |
| `Agent(execution_backend=...)` with only shell support | A coherent `.shell`/`.fs` backend; `from linch import LocalExecutionBackend, RemoteExecutionBackend` |
| `BashTool(backend=...)` | `BashTool(execution=...)`; `from linch import BashTool` |
| `coding_tools`, `default_tools`, `defaultTools`, `tools_from_defaults` | `workspace_tools(exclude=..., extra=...)`; `from linch import workspace_tools` |
| `ToolRegistry.add/remove` | `register/unregister`; `register` and `replace` return reversible `Disposable` handles; `from linch import ToolRegistry` |
| `permissions["canUseTool"]` and `.canUseTool` | `permissions["can_use_tool"]` and `.can_use_tool` |
| Tool `.schema` attribute | Tool `.input_schema` attribute |
| AskUser response `ok` / `confirmed` keys | `accepted`, or `answers`-only response inference |
| Callback, hook, and pipeline `policy_id` / `policy_version` attributes | `resume_policy_id` / `resume_policy_version` |
| `Session(provider_view=..., full_history=...)` | `session_log=SessionLog.seed(historical=..., visible=...)`; `from linch import Session, SessionLog` |
| `SessionLog.derive_messages()` | Read `session.provider_view`; append a list with `await session.append([message, ...])` |
| Mutation of `provider_view` / `full_history` | Detached sequence snapshots; append messages or record a logged projection |
| `AgentMiddleware` | Duck-typed hooks through `ToolMiddlewareHook`; `from linch import ToolMiddlewareHook` |
| Built-in `Read`/`Write`/`Edit`/`Glob` argument aliases (`path`, `old`, `new`, `pattern`) | Canonical `file_path`, `old_string`, `new_string`, `glob_pattern`, and `target_directory` keys |
| Background completion writing directly into notification lists | Custom sessions implement async `notify(...)` |
| `parallel_safe` fallback | Declare `parallel`: read tools use a boolean opt-in; an input-aware predicate may also admit calls in another scope, such as a subagent restricted to read tools |
| Ambiguous worker display names for `SubagentContinue` or `TaskStop` | Pass the unique `worker_id`; unique display names remain accepted |
| Task tools keyed by the calling session, so a worker saw an empty `TaskList` | `TaskCreate`, `TaskList`, `TaskGet`, and `TaskUpdate` use the root session of the agent tree, found through each child session's `parentSessionId` metadata |
| `TaskStop` marking any worker `killed` and only echoing `reason` | It stops only a `running` worker and records `reason` in `WorkerHandle.stop_reason`; a finished worker keeps its status and the result says it was not running |
| A continue to a running worker marking it `failed` | The continue returns a `WorkerBusy` error and leaves the handle unchanged; a worker reads `running` while a continuation drives it |
| `continue_subagent()` reporting `sa_cont_<worker_id>` as every continuation's `subagent_run_id` | Hooks and `SubagentEvent` carry the `ContinueSubagentArgs.subagent_run_id` supplied by the caller; `SubagentContinue` generates a fresh one per call |
| Worker subagents offered `SubagentContinue` and `TaskStop` | Only the session that spawned workers has them; every worker catalog omits `Subagent`, `SubagentContinue`, and `TaskStop` |
| `Agent(mailbox=...)` | Mount the plugin: `await agent.use(MailboxPlugin(), mailbox)`; `from linch import MailboxPlugin` |
| `Agent(ask_user=...)` | Mount the plugin: `await agent.use(AskUserPlugin(), handler)`; `from linch import AskUserPlugin` |
| Large tool or skill catalogs | `ToolSearchPlugin` is mounted when the first session is created; use `await agent.tool_search.dispose()` to restore the unfiltered catalog, or mount `ToolSearchPlugin(max_tools=..., max_skills=...)` explicitly |
| `Agent(tools=registry)` sharing later caller mutations | Each agent makes a shallow registry copy at construction (the tool instances are shared); register later tools on `agent.tools` to keep agent registrations isolated |
| `Agent(tools=[...])` | `Agent(tools=empty_tools(...))`; `empty_tools` creates a `ToolRegistry` containing only the supplied tools; import `Agent`, `BashTool`, and `empty_tools` from `linch` |
| Missing-`jsonschema` validation bypass | `jsonschema` is core; configured output schemas are always validated |
| Persisted skill fields `substitutedBody` and `invokedAt` | Canonical `substituted_body` and `invoked_at`; malformed records are rejected |

For example, replace a 2.x tool list with an explicit registry:

```python
from linch import Agent, BashTool, empty_tools

agent = Agent(model="gpt-5", tools=empty_tools(BashTool()))
```

Constructor aliases now use these exact spellings:

| Removed alias | Replacement |
|---|---|
| `systemPrompt` | `system_prompt` |
| `maxRetries` | `max_retries` |
| `maxOutputTokens` | `max_output_tokens` |
| `includePartialMessages` | `include_partial_messages` |
| `maxTurns` | `max_turns` |
| `maxToolConcurrency` | `max_tool_concurrency` |
| `toolBatchingStrategy` | `tool_batching_strategy` |
| `toolTimeoutMs` | `tool_timeout_ms` |
| `cacheTtl` | `cache_ttl` |
| `configDir` | `config_dir` |
| `mcpServers` | `mcp_servers` |
| `loopGuard` | `loop_guard` |

Tool-context aliases `sessionId`, `runId`, `sessionStore`, `fileReadTracker`,
`idempotencyKey`, and `reportProgress` become `session_id`, `run_id`,
`session_store`, `file_read_tracker`, `idempotency_key`, and `report_progress`.
Middleware aliases `sessionId`, `runId`, `turnIndex`, `toolUseId`, and `toolName`
become `session_id`, `run_id`, `turn_index`, `tool_use_id`, and `tool_name`.

## Explicit extension imports

Only the replacement imports in the table are available in 3.0; update the
old names in the first column at their call sites.

| Old package or root exports | Replacement import |
|---|---|
| `workflow`; root workflow context/journal/result/error helpers | `from linch.ext.workflow import WorkflowContext, WorkflowJournal, StepOutcome, WorkflowError, WorkflowSuspended, WorkflowTimeoutError, call_key, interrupt_key, step_key, run_workflow` |
| `Agent.run_workflow(fn, ...)` | `await run_workflow(agent, fn, ...)` from `linch.ext.workflow` |
| `evals`; root harness exports | `from linch.ext.evals import EvalCase, CaseResult, EvalResult, run_eval` |
| Root eval suite/benchmark exports | `from linch.ext.evals import EvalSuite, EvalBenchmarkTarget, EvalTargetResult, EvalBenchmarkResult, load_eval_suite, load_scripted_turns, run_eval_benchmark` |
| Root scripted-provider exports | `from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn` |
| Root eval scorers | `from linch.ext.evals import text_contains, tool_called, schema_valid, cost_under, context_selected_tool, context_not_trimmed, context_metadata_contains, recovery_succeeded, run_completed` |
| `text_contains` scorer fields `text` / `contains` | `substring` (omitted field defaults to `{expected}`) |
| `deep_agent`; root preset/profile/prompt exports | `from linch.ext.deep_agent import create_deep_agent, DeepAgentProfile, DEEP_AGENT_PROFILES, DEEP_AGENT_SYSTEM_PROMPT, DEEP_AGENT_SUBAGENTS` |
| `reports`; root report helpers | `from linch.ext.reports import RunReport, build_run_report, load_run_report` |
| `loop_runner`; root loop spec/runner exports | `from linch.ext.loop_runner import LoopSpec, LoopTrigger, LoopTickResult, LoopRunner` |
| Root loop artifact/lease exports | `from linch.ext.loop_runner import LoopArtifactStore, LoopLease, LoopLeaseStore, FileLoopArtifactStore, FileLoopLeaseStore, InMemoryLoopLeaseStore` |
| `cli` / its `main` console entrypoint | `from linch.ext.scaffold import main`; console target `linch.ext.scaffold:main`; the `linch` command remains |

For example:

```python
from linch import Agent, InMemorySessionStore
from linch.ext.deep_agent import create_deep_agent
from linch.ext.evals import EvalCase, ScriptedProvider, TextTurn, run_eval
from linch.ext.loop_runner import LoopRunner, LoopSpec
from linch.ext.reports import RunReport, build_run_report
from linch.ext.workflow import WorkflowContext, run_workflow
```

`create_deep_agent(durable=True)` requires explicit `session_store`,
`run_store`, and an explicit `filesystem` when enabled. Use Postgres adapters
for restart durability or `durable=False` for an ephemeral preset. Explicit
InMemory overrides are accepted, but retain state only for the process lifetime.

## Postgres stores, leases, and resume

Install `linch[postgres]`. Hosts read configuration and pass DSNs explicitly;
the SDK does not discover DSNs from the environment.

| Removed or changed | Replacement |
|---|---|
| `SqliteRunStore` | `from linch import PostgresRunStore, InMemoryRunStore` |
| `SqliteSessionStore` | `from linch.sessions.postgres import PostgresSessionStore`; `from linch import InMemorySessionStore` for tests/dev |
| `SqliteFileBackend` | `from linch.filesystem.postgres import PostgresFileBackend`; `from linch import StateFileBackend` for ephemeral files |
| `SqliteMailbox` | `from linch import InMemoryMailbox, Mailbox`; durable mailbox adapters are host-owned |
| Incompatible persisted schemas or state | Start with a current Postgres schema and new runs; built-in stores reject incompatible state |
| `RunOptions.allow_legacy_resume` and contractless resume | A current execution contract and wire generation 3; `from linch import RunOptions` |
| Silent resume of terminal runs or runs without checkpoints | Raises `RunNotResumableError` with `run_id` and `status`; `from linch import RunNotResumableError` |
| Cursorless assistant-boundary tool recovery | A valid `tool_batch_event_after_seq` cursor is required |
| `scripts/run_report.py --runs-db` | `--dsn` or host script environment `LINCH_POSTGRES_DSN`; `load_report` / `render_report` now take a Postgres DSN |
| Offloaded tool results written to the session filesystem (an execution world's workspace, or an in-memory backend lost on takeover) | Kept in the session store under the root session of the agent tree, whatever filesystem backend is configured; `PostgresSessionStore` adds the `session_offloads` table, and a custom store without `save_offload` / `load_offload` / `list_offloads` keeps results inline |

All persisted envelopes share generation **3** (`linch.RUN_SCHEMA_VERSION`):
checkpoints, run contracts, model-call intents/receipts/journals, exact model
input, permission decisions, session/provider-view snapshots, events, and
workflow metadata. Missing, pre-3.0, and unknown versions are rejected. Unknown
required events fail; only valid unknown envelopes marked
`ignorable: true` round-trip as `IgnorableEvent`.

Custom store implementations must follow these changed contracts:

- `RunStore.create_run(..., initial_checkpoint=...)` atomically persists the
  checkpoint with creation or adoption. An existing ID is adopted only for a
  same-session `pending` run, with metadata merging and contract checking;
  other existing IDs raise `ValueError`.
- `mark_failed(..., events=())` commits a failed status and pending terminal
  events together and returns only the event seqs.
- The checkpoint writes no longer return a `RunRecord`: `save_checkpoint` and
  `mark_completed` return `None`, `save_checkpoint_with_events` and
  `mark_failed` return the event seqs. Use `load_run` to read stored state.
- `RunStore.save_checkpoint_with_events(...)` atomically commits a checkpoint
  and its pending events; this is required when a phase has pending events.
- Run state is group-committed at barriers instead of written through per
  phase: provider dispatch, paid receipts and projections, each tool start and
  end, session-store writes, and terminals. A tool turn makes 7 store writes
  instead of 14. Intermediate checkpoint phases (`assistant_appended`,
  `tool_batch_pending`, `tool_results_appended`, `turn_complete`) usually reach
  the store only inside the next barrier's write, so a crash between barriers
  resumes from the last barrier. A crash before a tool-start commit asks the
  permission callback again.
- `ModelInputSnapshotStore.delete` requires `run_id` for fenced cleanup.
- Postgres schemas require durable event/message sequence counters. Lease
  capabilities fence writes with owner/epoch; workers drive renewal. Import
  `RunLease`, `RunLeaseStore`, `RunLeaseConflictError`, and `LeaseLostError`
  from `linch` when implementing ownership handling.
- Leases are carried per write, not held by the store. Every run-store
  mutation takes an explicit `lease=` token; pass the claimed `RunLease`
  through `RunOptions(lease=...)` when driving a leased run yourself.
  Stores no longer remember which leases they hold. A custom leased store
  must accept `lease=` on every mutation and validate run, owner and epoch in
  the same operation as the write. `RunLease` gains `ttl_s`, used for renewal.
  Unleased writes stay supported for in-process runs.

Writes remain durable before guarded side effects. Lease loss ends execution
without further writes. Transient durable-store connection failures interrupt
resumable runs for worker retry/takeover rather than writing a failed terminal.
See [Run stores](usage/run-stores.md) and [Production](usage/production.md).

## Worker dispatch and schedules

Install `linch[worker,postgres]` and import the runtime explicitly:

```python
from linch.worker import Worker, enqueue_run, register_schedule_tools
```

| Removed scheduling API | Replacement |
|---|---|
| `Schedule`, `ScheduleOccurrence`, `ScheduleStore` | `from linch.worker import ScheduleDefinition, create_schedule, list_schedules, cancel_schedule` |
| `ClaimingScheduleStore`, `LeasedScheduleStore`, `InMemoryScheduleStore`, `SqliteScheduleStore` | Worker/Postgres schedule definitions and PgQueuer job claims/retries |
| `SchedulerLoop` | `Worker` and PgQueuer dispatch |
| `schedule_tools` | `register_schedule_tools(...)`, returning a `Disposable` |
| `Agent(schedule_store=...)` | Hosts register worker schedule tools explicitly |
| `render_schedule_message` | Schedule fires dispatch separate runs |
| `cron_matches`, `next_cron_time`, `validate_cron` | Worker cron definitions use `croniter`; no core scheduling helpers |

Worker factories must reuse both `job.session_store` and `job.run_store`.
These adapters are initialized once per worker. `enqueue_run` provides
idempotent queue submission; leases fence execution independently of dispatch.
See [Scale out](usage/production.md#scale-out).

## File memory

Use filesystem files for explicit memory: an index at `/memories/MEMORY.md`,
bounded to 200 lines, and one file per fact. Scope the backend with
`ScopedFileBackend` in `RunOptions(deps=...)`.

```python
from linch import (
    ContextInjectionHook,
    FileMemoryContextBuilder,
    ScopedFileBackend,
    StateFileBackend,
    assert_file_backend_contract,
)
```

| Removed memory API | Replacement |
|---|---|
| `MemoryStore`, `PostgresMemoryStore`, `TieredMemoryStore` | A scoped `FileBackend`; production may use `PostgresFileBackend` |
| `InMemoryKeywordMemoryStore` | `StateFileBackend` for test/dev memory files |
| `MemoryContextBuilder` | `ContextInjectionHook(FileMemoryContextBuilder())` |
| `MemorySearchTool`, `MemoryUpsertTool` | Filesystem tools read/write fact files |
| `MemoryItem` | One fact per file |
| `assert_memory_store_contract` | `assert_file_backend_contract` |

See [Context and memory](usage/context-and-memory.md) for the optional mem0
recipe. File-memory index loading happens at most once per run.

## OpenTelemetry

OpenTelemetry API instrumentation is part of the core dependency set. Hosts
install/configure `opentelemetry-sdk` and an exporter; `Agent(context=...)`
accepts scoped tracer and meter services.

| Removed API | Replacement |
|---|---|
| `RunObserver`, `BaseObserver`, `observers=` | OTel for telemetry; native hooks for behavior |
| `ObserverDispatcher`, `normalize_observers` | SDK span processors/exporters |
| `LoggingObserver` | Configure the host OTel SDK's `ConsoleSpanExporter` |
| `SpanCollector` | Configure the host OTel SDK's `InMemorySpanExporter` |
| `OpenTelemetryObserver`, `RunTelemetryHook` | Direct runtime OTel instrumentation; remove the hook |
| `RunInfo`, `RunResultInfo` | `invoke_agent` span attributes or agent lifecycle hooks |
| `TurnInfo` | Step spans or turn hooks |
| `ProviderCallInfo`, `ProviderCallResult` | `chat` spans or provider-call hooks |
| `ToolInfo`, `ToolResultInfo` | `execute_tool` spans or tool hooks |
| `Span` | Use the host OTel SDK's `ReadableSpan` type |
| No run trace correlation on boundary events, tools, or logs | `SystemEvent.trace_id`, `ResultEvent.trace_id`, read-only `ToolContext.trace_id`, and opt-in `TraceIdFilter` |
| `examples/observability/custom_observer.py` | `examples/observability/observability_agent.py` |

Content capture requires `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`.
No host SDK means the API is a no-op. See [Events and telemetry](usage/events.md).

Kernel lifecycle dispatch sites use a generated typed catalog; the bus rejects
a mode mismatch while user-facing hook methods keep their ordering. See the
generated catalog in
[architecture/events.md](architecture/events.md#typed-lifecycle-catalog).

Behavioral hook contexts remain available independently of telemetry. Register
hooks with `Agent(hooks=[...])`; use the supported top-level hook contexts and
`HookResult`.

## Gemini

The `gemini` extra now installs `google-genai` in place of
`google-generativeai`. Update host integrations to the `genai.Client` API and
the current SDK types. Linch's provider imports remain:

```python
from linch import GeminiProvider, GeminiProviderOptions
```

`GeminiProviderOptions.project` selects Vertex AI; `location` and application
default credentials apply there. `default_generation_config` accepts native
`GenerateContentConfig` fields, including `thinking_config`. Signed model parts
are retained for replay. The current options are `api_key`, `project`,
`location`, and `default_generation_config`.

For host code using SDK objects directly:

| Old SDK API/configuration | Replacement |
|---|---|
| `genai.configure` / `GenerativeModel` | `genai.Client` |
| `generate_content_async` | `client.aio.models.generate_content_stream` |
| Tool schema key `parameters` | `parameters_json_schema` |
| Structured-output key `response_schema` | `response_json_schema` |

The Linch adapter disables automatic SDK function calling; Linch schedules
and authorizes tools. See [Providers](usage/providers.md).

## Reliability, identity, and retrieval

`Agent` defaults to `ReliabilityPolicy()` for bounded invalid-response
corrections; pass `reliability=None` to opt out. `ToolPrerequisite` and
`CompletionGate` use successful foreground calls in the current run. With the
default policy, whole-text `<tool_call>` envelopes request a native resend and
never execute tools.
Reliability policy is part of the resume contract; changing it requires a new
run. The extension seam is the serial-veto turn-stopping event.

`RunOptions(principal=Principal(...))` supplies immutable tenant/user/role
identity. `ToolRule`, `PathRule`, and `BashRule` accept `role=` and
`unless_role=`. Resume restores an omitted principal and rejects a different
explicit identity; role order is canonicalized. Hosts authenticate and supply
the identity and roles.

Retrieval uses `RagConnector` / `RetrievedChunk`, installed with
`register_rag_connector`. `register_grounding_check` checks `[doc:<id>]`
citations against retrieved IDs before finalization. Registrations return
`Disposable` handles.

```python
from linch import (
    CompletionGate,
    Principal,
    RagConnector,
    ReliabilityPolicy,
    RetrievedChunk,
    RunOptions,
    ToolPrerequisite,
    register_grounding_check,
    register_rag_connector,
)
```

See [Permissions](usage/tools.md#per-run-principals-and-role-rules),
[RAG](usage/context-and-memory.md#sourced-retrieval-and-grounding), and
[Agent configuration](usage/agent.md).

## Accounting and compaction

### Accounted usage is cumulative

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

`UsageEvent` reports primary usage. Auxiliary compaction usage adds
`source="compaction"`, `call_id`, `logical_call_id`, and `model`. The public
`ModelCallEvent` / `is_model_call_event` pair exposes the typed journal envelope
for store and journal observers; ordinary primary delivery records remain
journal-only and are not extra normal stream events.

`ResultEvent.total_usage` and run reports expose the cumulative known total. If
a dispatched delivery is cancelled, fails, or ends before valid final usage,
the usage is unknown rather than zero and the terminal result sets
`usage_complete=False`. An uncapped run may finish with that lower bound.

### Budget behavior changes at the compaction boundary

Compaction is charged against the same run budget as the primary turn. Receipt
settlement happens before summary application, hooks continue, or another
provider dispatch. The loop rechecks admission after compaction, so a summary
that exhausts a token or USD cap prevents the main turn from starting.

For a capped run, unknown usage is a strict fence: Linch stops before another
model call rather than claiming that the configured cap is enforced. A
pre-dispatch cancellation or admission denial is not billed; a dispatched call
without valid final usage is not silently treated as free. A cap is checked at
admission, not reserved in advance, so a delivery already admitted can settle
at or above the configured cap; equality exhausts the cap for the next
admission.

The same live `RunBudget` object shares receipt ownership and charged delivery
IDs across the in-process agent/subagent tree. A resume may adopt a checkpoint
with a fresh zero-spent budget; a fresh nonzero or owner-mismatched capped
budget fails closed rather than replaying charges. These are in-process
guarantees only: distributed-tree recovery, exactly-once external-provider
billing, and reservations against concurrent admissions are not promised.

### Durable compaction replay

`DefaultCompaction` and `DetailedCompaction` use their configured strategy
semantics and prompts. In a durable run, an accepted summary carries a bounded
receipt-backed result and an ordered hash-only source recipe. Required invoked
skill bodies are flushed through the configured session store before the recipe
is written. On resume, a matching source, strategy configuration, run
contract, and canonical skill-listing wrapper allow Linch to apply the stored
summary without another provider call or duplicate charge.

Linch fails closed when the source order, durable history boundary, receipt,
skill body, listing wrapper, or contract does not match. A provider-view cache
is verification material, not source authority.

### Custom and standalone compaction

A custom strategy implementing the public `CompactionStrategy` protocol and
using the provider facade receives accounting, run cancellation, and one
provider-slot/limiter acquisition per physical stream. Opaque network calls
outside that facade are not observable by the SDK. Arbitrary multi-call or
transform behavior has no automatic deterministic paid-result replay guarantee;
provide an explicit deterministic result/commit seam if the host needs one.

The standalone `summarize_with_provider(...)` helper streams directly through
the supplied provider. Calling it outside the normal run does not create a
durable run receipt or add usage to a `RunBudget` unless the host integrates
that accounting itself.

### Pricing and compatibility limits

Unknown or unpriced models never receive a synthetic USD price. Their reported
tokens remain in cumulative usage, while cost fields and known-cost aggregates
are partial (`None` where no price is known). A USD cap cannot enforce an
unknown price; use a custom pricing table when the host knows the model's
rates.

See [Events](usage/events.md), [Agent & session](usage/agent.md), and
[Compaction architecture](architecture/compaction.md) for the operational
details.

## Internal cleanup for adapter maintainers

Adapter code must target the current wire-generation-3 contracts. Persisted
state that fails generation, checkpoint, receipt, or execution-contract
validation is rejected. Custom stores must implement the public atomic
checkpoint/event and lease contracts above. Do not add compatibility readers or
aliases.
