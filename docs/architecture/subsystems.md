# Subsystems

> Use this page when you need a subsystem contract, execution boundary, or
> cross-cutting invariant.

> Part of the [Linch architecture guide](./README.md).

This page describes the current Linch 3.1 runtime. The kernel is headless and
provider-agnostic; a host supplies the process lifecycle, HTTP or RPC
transport, tenant services, and the OpenTelemetry SDK.

## 3.1 Kernel and scoped services

`linch.kernel` is a small dependency-free service and effect kernel. An
`Agent` owns its per-agent `Context` scope containing its service registry,
event bus, and reversible effects. When a host `Context` is supplied, that
scope is a child and the host parent remains host-owned. `Disposable` handles
undo one registration and are idempotent. The same primitives are used by tool
registration, hooks, turn-stopping listeners, and optional adapters.

The kernel has no process-global mutable registry. A host can create several
agents in one process; each agent has its own tools, hooks, sessions, permission
engine, run configuration, and telemetry resolution. A supplied parent
`Context` shares host services through a child scope, so closing an agent does
not dispose the host scope.

See [kernel.md](./kernel.md) for the `Context`, `Disposable`, effect-scope, and
event-bus contracts.

## 3.2 Sessions, projections, and turn phases

`Session` is the per-conversation state holder. Its `SessionLog` is the single
append-only source for two read projections:

- `full_history` is the audit projection;
- `provider_view` is the model-visible projection after logged compaction.

Per-request context from `ContextInjectionHook` and RAG builders is merged into
the `ProviderRequest` only. It is not appended to either projection. Grow the
history through `await session.append([...])`; the method accepts an ordered
`list[Message]`. A direct
`SessionLog.append(...)` is an in-memory mutation and is lost on reload.
Compaction uses `session.session_log.record_projection(...)`, keeping the
projection change in the log while preserving the audit history.

The loop is split by responsibility:

1. `loop/startup.py` appends the prompt and restores checkpointable state.
2. `loop/provider_turn.py` builds context and a provider request, streams a
   normalized provider response, and appends an accepted assistant message.
3. `loop/tool_batch.py` validates and executes an assistant tool batch, appends
   tool results, and evaluates `LoopGuard`.
4. `loop/finalize.py` handles final text and terminal-tool output.
5. `loop/turn_stopping.py` runs the serial terminal decision seam before a
   terminal result or final-tool acknowledgement is committed.
6. `loop/checkpoint.py` and `loop/runtime.py` persist phases, events, model-call
   receipts, extension state, and resume cursors.

```mermaid
sequenceDiagram
    participant Caller as Host caller
    participant S as Session
    participant R as RunStore
    participant P as Provider
    participant T as Scheduler

    Caller->>S: run(prompt) or resume(run_id)
    S->>R: create/load run + initial checkpoint
    loop each turn
        S->>R: checkpoint provider_pending
        S->>P: stream ProviderRequest
        P-->>S: normalized text/tool events
        alt tool calls
            Note over S,R: tool_batch_pending is held, but exact-input runs write it at once
            S->>R: commit tool_executing + pending events
            S->>T: resolve, authorize, execute tools
            T-->>S: ToolCallStart/End events
            S->>R: append tool results + checkpoint
        else terminal candidate
            S->>S: dispatch TurnStopping
            S->>R: append result + completed checkpoint
        end
        S-->>Caller: typed event stream
    end
```

When a run is durable, the checkpoint is written before the operation whose
outcome needs recovery. A tool integration therefore receives a stable
`ToolContext.idempotency_key` (`run_id:tool_use_id`) and must reconcile an
external side effect if a crash occurs after the side effect but before its
completion event. Linch provides at-least-once recovery, not exactly-once
external side effects.

`Agent(max_idle_sessions=N)` is an optional LRU bound for idle sessions. Active
runs, pending notifications, and live background work protect their ownership
tree from eviction.

## 3.3 Durable runs, leases, and checkpoints

The required `RunStore` protocol is in `run_store.py`. It persists
`RunRecord`, `RunCheckpoint`, status, and a per-run ordered event stream. The
optional capabilities are detected at runtime:

- `RunLeaseStore`: `claim(run_id, owner, ttl_s)`, `renew(lease)`, and
  `release(lease)`;
- `RunEventBatchStore`: atomic append of several events; and
- `ModelInputSnapshotStore`: exact provider-request snapshots for pending
  provider replay.

`InMemoryRunStore` implements the same current wire and fencing semantics for
tests and development. `PostgresRunStore` is in `run_stores/postgres.py` and
uses the Postgres extra. It owns three relevant tables:
`runs` (status, checkpoint, lease columns, and last event sequence),
`run_events` (unique `(run_id, seq)`), and `model_input_snapshots`.

A partial unique index on `runs(session_id)` admits one nonterminal run:
`pending`, `running`, `waiting_permission`, and `suspended` all block another
run with `SessionBusyError`. The in-memory store enforces the same rule.
Initialization installs this index on compatible version-3 schemas and rejects
duplicate nonterminal rows; hosts reconcile them with the old deployment before
upgrading. Worker queue-task concurrency defaults to 32 per process (minimum two),
independently of fleet queue limits. Task cancellation preserves a nonterminal
row for resume; `session.abort()` followed by consuming the final result records
an aborted run and frees admission. In-process interruptions have no reaper.

Run-owned process groups and shell spill files are cleaned before a durable
terminal status (`completed`, `aborted`, or `failed`) is recorded. Cleanup
failure is surfaced, leaving admission nonterminal and `_active` retained for
retry. `waiting_permission` and `suspended` remain resumable.

Resume identity excludes the package version and top-level tool descriptions,
while wire generation, schemas, scope/concurrency, and execution policy remain
significant. The 3.1.0 backend policy version `2` and Bash schema changes require
a drain to zero active runs before deployment, including suspended runs.

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: create/adopt
    running --> waiting_permission: permission request checkpoint
    waiting_permission --> running: resume
    running --> suspended: workflow extension
    suspended --> running: resume
    running --> completed: terminal result
    running --> failed: ErrorEvent + failed checkpoint
    running --> aborted: cooperative abort
```

### Lease fencing

`RunLease` contains `run_id`, `owner`, `epoch`, `expires_at`, and `ttl_s`. A
Postgres claim is a conditional `UPDATE ... RETURNING`: an unexpired lease owned
by a different worker returns `RunLeaseConflictError`; a takeover increments the
epoch. Renewal keeps the epoch and uses the token's TTL, while release clears
ownership and advances the epoch. Every checkpoint, status, event, and
model-input-snapshot mutation accepts an explicit `lease=` token and includes
its owner and epoch predicate. A stale holder receives `LeaseLostError` and
cannot append an event or change status. Releasing a token permanently fences
that token; unleased mutations remain supported for runs without worker
ownership.

Stores without the optional lease capability, or runs started without a held
lease, retain the single-process behavior of the `RunStore` protocol.

### Checkpoint and event ordering

`RunEventBuffer` (`loop/checkpoint.py`) group-commits run state. Running
checkpoints and events stay in memory until a barrier commits them in one
`save_checkpoint_with_events()` write:

- `flush()` before provider dispatch, before each `ToolCallStartEvent` is
  yielded (with the `tool_executing` checkpoint, so the tool cannot run first),
  after each `ToolCallEndEvent`, and at terminals;
- `flush_checkpoint()` before every session-store write (`durable_call` ops
  named `session.*`); it commits only the held checkpoint and the events it
  covers.

A held checkpoint is a snapshot taken at its save together with the count of
events pending then, so every committed state is one the write-through order
also produced. Dispatch markers, paid receipts and projections are written
immediately. Startup preparation (`started`), exact-input runs, queued host
alignment and non-running statuses still write through. Intermediate phases
(`assistant_appended`, `tool_batch_pending`, `tool_results_appended`,
`turn_complete`) usually reach the store only inside the next barrier's write,
so a crash between barriers resumes from the last barrier. A started tool with
no durable end resumes with an interrupted-tool result and is not re-run.

`RunCheckpoint.tool_batch_event_after_seq` is the recovery cursor for tool
results. `load_events(run_id, after_seq=...)` returns ascending `StoredRunEvent`
rows. A stream reader stores its last delivered sequence and reconnects from
that sequence; a missing notification never means that an event is missing.

Current persisted envelopes use wire version 3. Missing, pre-3.0, unknown, or
contractless run data is rejected. `storage/_pg.py` coordinates Postgres
schema verification under advisory locks.

## 3.4 Worker dispatch and PgQueuer recovery

`linch.worker` is an optional runtime, installed with `linch[worker]` and
imported explicitly. Core `linch` does not import PgQueuer. The host supplies
an asyncpg pool and calls `install(pool)` once per process startup.

`enqueue_run()` and `enqueue_runs()` create pending run rows and PgQueuer jobs
in one transaction. A caller-supplied run id is a permanent deduplication
receipt. Queue names map to PgQueuer entrypoints such as
`linch.run:default`; concurrency limits are enforced by the database across
the fleet. `create_schedule()` uses a deferred `linch.schedule` job, UTC
croniter validation, deterministic fire run ids, and a dedupe key per
occurrence. Schedule tools are mounted explicitly with
`register_schedule_tools()`.

`Worker` receives an `AgentFactory` callable whose argument is a `WorkerJob`.
The factory must return a fresh agent for the job and reuse the job's
`PostgresRunStore` and
`PostgresSessionStore`; those stores borrow the worker's host pool. The worker:

1. verifies schemas and installs PgQueuer before dequeuing;
2. claims a Linch run lease and starts a heartbeat at the lease TTL / 3;
3. runs a new prompt or resumes the saved checkpoint;
4. cancels the run task on lease loss or expiry without writing a terminal
   status under a stale epoch;
5. quiesces the session, releases the lease, and checks that the run is
   terminal before allowing PgQueuer to acknowledge the job.

Transient Postgres, listener, and queue-manager failures rebuild the manager
on a fresh connection with bounded jittered backoff. PgQueuer's stale-job
recovery re-picks jobs whose queue heartbeat stopped; the next worker then
claims the expired run lease and resumes from its checkpoint. Setup failures
that are not infrastructure errors are committed as a failed run with error
metadata, an `ErrorEvent`, and a final `ResultEvent`. Infrastructure failures
remain retryable through `DatabaseRetryEntrypointExecutor`.

`Worker.stop()` prevents new dequeue operations and drains active jobs. The
host keeps the pool open until `Worker.run()` returns. `PostgresRunStore` can
also be used directly by an API stream reader; poll `run_events` by sequence
when a notification channel is unavailable or too contended.

## 3.5 Tool scheduler and execution security

`scheduler.py` is the single tool-execution chokepoint. For every provider
tool-use block it performs this ordered pipeline:

```mermaid
flowchart LR
    Resolve["resolve + validate"] --> Offered["offered-tool boundary"]
    Offered --> Pre["PreToolUse / middleware"]
    Pre --> Revalidate["validate transformed input"]
    Revalidate --> Permit["PermissionEngine"]
    Permit --> Checkpoint["tool_executing checkpoint"]
    Checkpoint --> Batch["resource-aware batches"]
    Batch --> Execute["tool.execute(ctx)"]
    Execute --> Post["PostToolUse"]
    Post --> Result["ToolCallEndEvent"]
```

The offered-tool boundary rejects a registered tool that was not included in
the current provider request. `PreToolUse` is the only input-mutation seam;
the transformed input is validated again and becomes the canonical input for
permission keys, summaries, resource declarations, approval callbacks, and
execution. A permission request is aggregated for the batch and the resolved
decisions are checkpointed before any allowed tool starts.

`scope="read"` tools with a true `parallel` flag can share a batch. A
`scope="write"` or `scope="exec"` tool with a boolean flag remains serial.
An input-aware callable may prove a particular call safe for parallel work in
any scope; predicate errors fail closed to serial. `ResourceAccess(resource,
mode)` prevents a write/read or write/write conflict. `max_tool_concurrency`
and `tool_batching_strategy` bound and shape the batches, while result blocks
are reassembled in provider order.

Tool timeouts and retry policy are applied at this chokepoint. Read calls can
retry configured exceptions; write/exec calls retry only when the tool declares
`retryable = True`. Background calls return an acknowledgement while their
task remains owned by the session and their completion is surfaced through the
session notification/event paths.

## 3.6 Providers, terminal decisions, and reliability

Providers implement `BaseProvider` and return normalized stream dictionaries;
the loop does not consume vendor SDK objects. The built-in adapters are
OpenAI Chat Completions, OpenAI Responses, Anthropic, DeepSeek, Gemini,
llama.cpp, vLLM, and SGLang. Gemini uses the optional `google-genai` package
(`linch[gemini]`). Provider capabilities downgrade unsupported request options
before dispatch.

`loop/turn_stopping.py` contains the serial decision seam used by final text,
final-tool, forced-final, and invalid-response routes. A listener returns
`TurnStoppingDecision("accept")`, `"continue"` with feedback, or `"fail"`
with an error. Continuation feedback is appended through the session log
before the next turn. Final-tool acknowledgement is emitted only after this
decision, so a listener can veto execution or request a correction.

The default `Agent` configuration installs `ReliabilityPolicy()`:

```python
from linch import Agent, ReliabilityPolicy

agent = Agent(model="your-model")
assert agent.reliability == ReliabilityPolicy()
agent_without_reliability = Agent(model="your-model", reliability=None)
```

The default policy retries invalid provider responses for at most two
correction turns. It executes no tools from a rejected native response.
`ToolPrerequisite` rules are installed as a `PreToolUse` hook, and
`CompletionGate` rules inspect successful foreground tool results. Both
reliability state and policy configuration are part of durable resume state.

The loop also has an enabled-by-default, opt-out `LoopGuard` on `Agent`: it tracks
repeated tool-call signatures and consecutive failure batches. The separate
`max_turns` run bound is enforced by the loop. The guard's force-final path
injects a reminder and removes tools from one final request.

## 3.7 Permission evaluation and `Principal`

`RunOptions.principal` carries an immutable `Principal` from the host into one
run and its tool calls:

```python
from linch import Agent, Principal, RunOptions, ToolRule

principal = Principal(
    tenant="acme",
    user="user-42",
    roles={"editor"},
)
agent = Agent(
    model="your-model",
    permissions={"rules": [ToolRule("write_file", "allow", role="editor")]},
)
session = await agent.session()
events = session.run("update the report", RunOptions(principal=principal))
```

`Principal` is defined in `permissions/principal.py`; its tenant, user, and
non-empty string roles are validated and frozen. `ToolRule`, `PathRule`, and
`BashRule` can select `role=` or exclude `unless_role=`. The
`PermissionEngine` passes the principal to `CanUseToolRequest`, and
`ToolContext.principal` exposes it to a tool. The run contract stores the
principal so a resume cannot silently switch tenant, user, or role identity.

This is host-configured RBAC policy, not a process-wide identity service. A
missing principal has an empty role set; hosts that need tenant isolation must
provide it on every new run and use rules that require the relevant role.

## 3.8 Context, file memory, and RAG

`ContextInjectionHook` receives a snapshot of `session.provider_view` and
returns a `ContextBuildResult`. System blocks, additional messages, selected
tools, metadata, and a `ContextBudget` are merged into the request and emitted
as a `ContextBuildEvent`; the session log is untouched. Builders perform async
I/O and can receive the per-run `deps` object.

`FileMemoryContextBuilder` is the current file-memory path. When mounted by a
`ContextInjectionHook`, it reads `/memories/MEMORY.md` once per run, keeps the
first 200 content lines, and injects a stable instruction plus the index. The
model uses the existing virtual filesystem tools to read, write, and edit one
fact per file. Backends also expose deletion to host code, but
`filesystem_tools()` registers no delete tool.

RAG is a small request-scoped protocol in `rag/__init__.py`:

```python
from linch import Agent, Principal, RetrievedChunk, RunOptions, register_rag_connector

class TenantRag:
    async def retrieve(self, query: str, *, k: int, principal=None):
        """Return an example sourced chunk for this request."""
        return [RetrievedChunk("doc-1", "handbook", "...", 0.9, {})]

agent = Agent(model="your-model")
run_principal = Principal("acme", "user-42", {"reader"})
undo = register_rag_connector(
    agent,
    TenantRag(),
    k=5,
    principal=run_principal,
)
session = await agent.session()
events = session.run("question", RunOptions(principal=run_principal))
```

`RagConnector.retrieve()` returns sourced `RetrievedChunk` values. The hook
adds `[doc:<id>]` system blocks and records the ids in checkpointable,
run-local state. `register_grounding_check(required=True)` adds a
`TurnStopping` listener that accepts only citations to the latest retrieved
ids; missing citations receive bounded correction feedback and then fail.
The active `RunOptions.principal` is passed to retrieval. A registration
`principal` is an optional expected-identity guard, so a missing or different
run principal fails closed before connector I/O.
RAG connectors and their clients are host-owned; Linch does not add an
embedding or vector-database dependency.

## 3.9 Virtual filesystem and result offload

The virtual filesystem is disabled by default through
`FeatureFlags(filesystem=False)`. Enabling it gives a session a
`StateFileBackend` unless the host supplies a backend. `filesystem_tools()`
returns `ls`, `read_file`, `write_file`, and `edit_file` tools. `DiskFileBackend`
and `PostgresFileBackend` are optional backend implementations; the latter is
imported from `linch.filesystem.postgres`.

`OffloadConfig` controls large-result offload. When enabled and a result is
above its resolved threshold, the scheduler writes the full content to the
virtual backend and puts a preview plus path hint in the model-facing result.
The full `ToolResult` remains on `ToolCallEndEvent.tool_result`. The default
offload configuration derives its threshold as 10% of the provider context
window; `preview_lines` defaults to 10. A backend write failure leaves the
original result unchanged.

Use a Postgres-backed composite filesystem when memory or offload files must
survive process restarts:

```python
from linch import Agent, CompositeFileBackend, FeatureFlags, StateFileBackend
from linch.filesystem.postgres import PostgresFileBackend

dsn = "postgresql://user:password@localhost/linch"
filesystem = CompositeFileBackend(
    default=StateFileBackend(),
    routes={"/memories/": PostgresFileBackend(dsn)},
)
agent = Agent(
    model="your-model",
    features=FeatureFlags(filesystem=True),
    filesystem=filesystem,
)
```

## 3.10 OpenTelemetry

Core depends on `opentelemetry-api`; a host chooses the SDK and exporter. The
private adapter in `_telemetry.py` resolves a tracer and meter through the
agent `Context` (or the global API providers when no scoped provider is
registered). With no SDK, calls are no-ops.

The kernel creates these spans at the I/O boundaries:

| Span | Boundary |
|---|---|
| `invoke_agent` | One run, including resume marker and run id. |
| `step` | One turn. |
| `chat <model>` | One provider stream, including usage and finish reason. |
| `execute_tool <name>` | One tool execution. |
| `compaction` | One compaction operation. |

Store calls are not spans. They record `linch.store.duration` with an `op`
attribute. The meter also records `gen_ai.client.token.usage` and
`gen_ai.client.operation.duration`. Span and metric attributes use the
`gen_ai.*` semantic-convention names plus `linch.*` run, turn, conversation,
resume, stop, and error attributes.

Prompt, tool arguments, tool results, and final content are captured only when
the host sets
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Otherwise the
adapter records identity, timing, usage, and error metadata without message
content.

For tests or per-agent instrumentation, register `tracer_provider` and
`meter_provider` on a `Context` before constructing the agent. The runtime does
not install an observer protocol or a custom span collector.

## 3.11 Extensions and host-owned boundaries

The core public surface contains the agent kernel, loop, providers, tools,
permissions, sessions, run stores, hooks, filesystem, and MCP adapters. The
following are explicit extensions under `linch.ext.*`:

- `linch.ext.deep_agent` for coordinator/worker presets;
- `linch.ext.evals` for scripted evaluation;
- `linch.ext.workflow` for deterministic journaled workflows;
- `linch.ext.reports` for report helpers;
- `linch.ext.loop_runner` for host-driven recurring loops; and
- `linch.ext.scaffold` for project scaffolding.

`coordination/` retains in-process mailbox messaging. Durable queue dispatch
and schedules live in `linch.worker`. Hosts own HTTP, provider credentials,
tenant dependency objects, RAG/vector clients, external side-effect
idempotency, and OpenTelemetry SDK lifecycle.

Back to the [architecture index](./README.md).
