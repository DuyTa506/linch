# Durable run stores

Use run stores when checkpoints and ordered run events must survive a restart.

A `RunStore` owns resumable run state: a run record, its latest checkpoint, and
an ordered event log. A `SessionStore` owns conversation metadata and its
append-only messages. Configure both on `Agent` when conversation history and
resumable runs must survive a process restart.

## Built-in backends

For run and session state, the SDK ships two process-local stores and two
durable adapters:

| Store | State lifetime | Use |
|---|---|---|
| `InMemoryRunStore` | Current process only | Tests and local development |
| `InMemorySessionStore` | Current process only | Tests and local development |
| `PostgresRunStore` | PostgreSQL | Durable runs and shared workers |
| `PostgresSessionStore` | PostgreSQL | Durable sessions and shared workers |

The built-in durable stores are PostgreSQL stores. Install the optional
dependency with `pip install 'linch[postgres]'`.

~~~python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

runs = PostgresRunStore("postgresql://user:password@localhost/linch")
sessions = PostgresSessionStore("postgresql://user:password@localhost/linch")
agent = Agent(model="gpt-5", session_store=sessions, run_store=runs)
~~~

The constructors have these exact signatures:

~~~text
PostgresRunStore(
    dsn: str,
    *,
    pool: Any = None,
    min_size: int = 1,
    max_size: int = 10,
)

PostgresSessionStore(
    dsn: str,
    *,
    pool: Any = None,
    min_size: int = 1,
    max_size: int = 10,
)
~~~

Constructing either adapter checks that `asyncpg` is installed. Pool and schema
initialization are lazy and happen on the first store operation. If pool is
supplied, the host owns that pool and the store never closes it; `dsn` is
ignored by the session adapter when a pool is supplied. A store-created pool
is closed by `await store.close()`. A host must keep a borrowed pool open until
all stores using it have stopped. If initialization of a store-owned pool or
schema fails, the adapter closes and resets that owned pool so a later attempt
can initialize again; a borrowed pool remains the host's responsibility.

Fresh PostgreSQL databases receive the current version-3 schema. Existing
databases whose Linch schema metadata is missing, old, unknown, or inconsistent
with the required shape are rejected without alteration; incompatible schemas
are not upgraded. On a compatible version-3 schema, initialization installs the
partial unique run-admission index. Duplicate nonterminal runs for one session
make initialization fail with `ConfigError`; use the old deployment to resume
or reconcile and mark failed those runs before upgrading stores/workers. A fresh
database is not required for this index installation. Run checkpoints/contracts/events, inbox receipts,
provider-view snapshots, and model-input snapshots use wire version 3.

## Store contracts

The protocols below are the implementation contracts. Successful mutations
are durable before they return. Values returned by either built-in store are
detached copies: mutating a returned record, message, task, checkpoint, event,
or nested metadata must not mutate stored state. Store implementations must
also detach mutable input graphs before retaining them.

Both built-in run stores admit only one nonterminal run per session. A second
run raises `SessionBusyError` with `session_id` and the existing `run_id`.
`pending`, `running`, `waiting_permission`, and `suspended` all block admission;
a terminal status frees it. Hosts queue new prompts or report busy. Cancelling
an in-process task preserves the durable nonterminal row: resume it or reconcile
and mark it failed. Use `session.abort()` and consume the final result when an
abort should end the run. See [production wiring](./production.md).

If a Postgres admission conflict disappears before it can be read, admission
tries up to three times outside a caller-owned transaction, or once inside one.
An unresolved conflict raises a chained error; retry the transaction or reconcile
the competing run.

### Run values

`RunRecord` contains:

- `id: str`
- `session_id: str`
- `status`, one of `pending`, `running`, `waiting_permission`, `suspended`,
  `completed`, `failed`, or `aborted`
- `created_at: str` and `updated_at: str` timestamps
- `checkpoint: RunCheckpoint | None`
- JSON-safe `meta: dict[str, object]`

`StoredRunEvent` contains a 1-based, per-run `seq`, an `appended_at` timestamp,
and a typed `event`. A `RunCheckpoint` preserves the execution phase, prompt,
turn index, cumulative usage, accepted assistant message, pending tool blocks
and results, force-final state, loop-guard state, pending skill overlay, allowed
tools, assistant stop reason, permission decisions, background-worker state,
truncation state, tool-batch event watermark, session-alignment state,
extension state, provider attempt, and referenced model-input snapshot. Its
phase is one of `started`, `user_appended`,
`provider_pending`, `assistant_appended`, `permission_pending`,
`tool_batch_pending`, `tool_executing`, `tool_results_appended`,
`turn_complete`, `workflow_suspended`, `completed`, `failed`, or `aborted`.
The store must round-trip every checkpoint
field, including extension-owned state it does not interpret.

The checkpoint fields and defaults are:

~~~text
phase: RunPhase
prompt: str
turn_index: int
total_usage: Usage
assistant_message: Message | None = None
pending_tool_blocks: list[ToolUseBlock] = []
completed_tool_results: dict[str, ToolResultBlock] = {}
force_final_pending: bool = False
loop_guard_state: dict[str, object] | None = None
pending_skill_overlay: dict[str, object] | None = None
current_turn_allowed_tools: list[str] | None = None
assistant_stop_reason: str | None = None
permission_decisions: dict[str, dict] = {}
background_workers: dict[str, dict[str, object]] = {}
truncation_attempts: int = 0
truncation_prefix: str = ""
pending_truncation_feedback: str | None = None
tool_batch_event_after_seq: int | None = None
pending_alignment: list[dict[str, Any]] = []
extension_state: dict[str, Any] = {}
provider_attempt: int = 0
model_input_snapshot_id: str | None = None
~~~

### Required RunStore methods

The required protocol has these exact signatures:

~~~text
async def create_run(
    session_id: str,
    *,
    id: str | None = None,
    meta: dict[str, object] | None = None,
    initial_checkpoint: RunCheckpoint | None = None,
    lease: RunLease | None = None,
) -> RunRecord

async def load_run(run_id: str) -> RunRecord | None

async def save_checkpoint(
    run_id: str,
    checkpoint: RunCheckpoint,
    *,
    status: str = "running",
    lease: RunLease | None = None,
) -> None

async def save_checkpoint_with_events(
    run_id: str,
    checkpoint: RunCheckpoint,
    events: Sequence[Event],
    *,
    status: str = "running",
    lease: RunLease | None = None,
) -> list[int]

async def append_event(
    run_id: str,
    event: Event,
    *,
    lease: RunLease | None = None,
) -> int

async def load_events(
    run_id: str,
    *,
    after_seq: int = 0,
) -> list[StoredRunEvent]

async def mark_completed(
    run_id: str,
    checkpoint: RunCheckpoint,
    *,
    lease: RunLease | None = None,
) -> None

async def mark_failed(
    run_id: str,
    checkpoint: RunCheckpoint | None = None,
    error: dict[str, object] | None = None,
    *,
    events: Sequence[Event] = (),
    lease: RunLease | None = None,
) -> list[int]
~~~

Implement each method as follows:

- `create_run` generates a UUID when `id` is omitted and creates a `running` record.
  `initial_checkpoint`, when supplied, is committed with that row. An explicit
  ID may adopt only a pending record belonging to the same session; adoption
  changes it to running, merges incoming metadata, and replaces its
  checkpoint. A non-pending record or a record owned by another session raises
  `ValueError`. If both the stored and requested metadata contain a run contract,
  an incompatible contract raises `RunContractMismatchError`. Creation and
  adoption detach and validate all inputs before changing state. A lease on a
  new row is invalid; a lease on adoption fences the existing pending row.
- `load_run` returns a detached record or `None` for an unknown ID.
- `save_checkpoint` replaces the checkpoint, stores the supplied `status`
  (default `"running"`) and updates `updated_at`. Write methods return no
  record; read the stored state with `load_run`.
  An unknown run raises `KeyError`.
- `save_checkpoint_with_events` commits the checkpoint/status and all supplied
  events as one phase boundary. Events retain input order. The returned
  sequence list is parallel to the input. Checkpoint or event encoding,
  database, or fencing failure leaves both the old checkpoint and the old
  event log unchanged.
- `append_event` appends one event and returns its new 1-based sequence.
- `load_events` returns detached rows in ascending sequence order. `after_seq`
  is an exclusive watermark; `0` reads from the beginning.
- `mark_completed` stores the supplied checkpoint with phase `"completed"` and
  status `"completed"`. It does not append events itself.
- `mark_failed` stores status `"failed"`; when a checkpoint is supplied, its
  phase is stored as `"failed"`, otherwise the existing checkpoint is retained.
  A supplied error is appended to the run's `meta["errors"]` history; malformed
  error history is replaced by a new list. Supplied terminal events are
  appended in the same atomic operation. Encoding, database, or fencing failure
  leaves status, checkpoint, metadata, and events unchanged.

Every required mutation accepting `lease` validates the token as part of the
same state change. Omitting it retains unleased behavior. The built-in stores
use a per-run counter for event allocation; PostgreSQL uses the run row's
`last_event_seq`, so concurrent writers do not allocate with a `MAX(seq)` read.
The `RunStore` protocol does not require `close`, but both built-in run stores
expose `async def close() -> None`; it is a no-op for `InMemoryRunStore` and
closes only a store-created pool for `PostgresRunStore`.

### Optional run-store capabilities

#### Batch event appends

A store may additionally implement this exact capability:

~~~text
async def append_events(
    run_id: str,
    events: list[Event],
    *,
    lease: RunLease | None = None,
) -> list[int]
~~~

When this optional capability is implemented, the batch is encoded and
committed in input order as one all-or-nothing
operation. Returned sequences are contiguous and parallel to events. An empty
batch returns `[]`; when a lease is supplied it is still validated. A non-empty
batch for an unknown run raises `KeyError`, a stale token raises `LeaseLostError`,
and an encoding failure leaves the event log unchanged. The loop detects this
capability and can otherwise use append_event.

#### Exact model-input snapshots

`ModelInputSnapshotStore` is an optional capability on a run store. Its exact
signatures are:

~~~text
async def save(
    run_id: str,
    provider_attempt: int,
    request: ProviderRequest,
    *,
    lease: RunLease | None = None,
) -> ModelInputSnapshot

async def load(snapshot_id: str) -> ModelInputSnapshot | None

async def delete(
    snapshot_id: str,
    *,
    run_id: str,
    lease: RunLease | None = None,
) -> None

async def prune(
    run_id: str,
    keep_ids: Sequence[str] = (),
    *,
    lease: RunLease | None = None,
) -> int
~~~

`ModelInputSnapshot` is immutable and contains `id`, `run_id`,
`provider_attempt`, `codec_version`, canonical `request_json`,
`integrity_hash`, and `created_at`.
Implementations must follow these rules:

- `save` requires an existing run and a positive, non-boolean integer
  `provider_attempt`. It validates the complete `ProviderRequest` before
  persisting anything. The snapshot uses codec version `3` and excludes the
  live `signal`. Its canonical JSON uses UTF-8, sorted keys, no insignificant
  whitespace, and disallows non-finite numbers. The payload covers the
  provider model, text system blocks, tools, messages, generation controls,
  reasoning/thinking settings, output schema, tool choice, partial-stream
  setting, and the optional `_native_catalog_changes` field; the latter is
  omitted when it is `None`.
- The canonical codec authorities are the public
  `create_model_input_snapshot(run_id, provider_attempt, request)` and
  `decode_model_input_snapshot(snapshot, signal=...)` helpers. A store persists
  the snapshot returned by the former and uses the latter to verify/decode it;
  it must not invent a second request encoding.
- The integrity value is `sha256:` followed by SHA-256 of
  `f"{codec_version}\n{request_json}"` encoded as UTF-8. The exact request
  codec rejects unsupported objects, non-string mapping keys, cycles, and
  non-finite numbers before the snapshot can be referenced by a checkpoint.
- `load` returns `None` for an unknown snapshot. It must verify the codec
  version, integrity hash, JSON shape, and typed request fields. Corruption, an
  integrity failure, or an invalid request shape raises
  `ModelInputSnapshotError`; an unsupported wire version raises
  `UnsupportedWireVersionError`.
- `delete` requires the owning `run_id` keyword and deletes only a snapshot whose
  stored `run_id` matches it. An unknown or differently owned snapshot is not
  deleted. PostgreSQL reports an unknown run as `KeyError`; the in-memory
  implementation has no row to delete and returns.
- `prune` deletes every snapshot belonging to `run_id` except IDs in `keep_ids` and
  returns the deletion count. An empty default retains none. PostgreSQL reports
  an unknown run as KeyError; the in-memory implementation returns zero when
  there are no matching snapshots.

`save`, `delete`, and `prune` fence their run mutation and snapshot mutation with
the same lease check. A stale lease changes neither the snapshot table nor the
run. The built-in in-memory and PostgreSQL run stores implement this capability.
For a custom store, unleased dispatch omits the lease keyword so an existing
unleased signature can continue to work; supplying a lease to a mutation that
cannot accept the keyword raises TypeError before that method is invoked.

When `DurabilityOptions.exact_model_input` is true, all four snapshot methods
are required; a missing `save`, `load`, `delete`, or `prune` capability
raises `ConfigError` before the run executes. The option defaults to `false`.
The `DurabilityOptions` defaults are `durable_inbox=False`,
`exact_model_input=False`, and `delivery_lease_s=30.0`; its `strict_v1()`
profile enables both capabilities.

## SessionStore contract

### Session values

`SessionRecord` has `id`, `created_at`, `updated_at`, application metadata
`meta`, and a list of persisted `invoked_skills` dictionaries. `StoredMessage`
has a 1-based, per-session `seq`, an `appended_at` timestamp, and a typed
`message`. Message sequences are ordered within each session.

### Required methods

The SessionStore protocol has these exact signatures:

~~~text
async def create(
    *,
    id: str | None = None,
    meta: dict[str, object] | None = None,
) -> SessionRecord

async def load(id: str) -> SessionRecord | None

async def load_messages(id: str) -> list[StoredMessage]

async def append_messages(
    id: str,
    messages: list[Message],
) -> list[StoredMessage]

async def update_meta(
    id: str,
    meta: dict[str, object],
) -> SessionRecord

async def set_invoked_skills(
    id: str,
    skills: list[dict[str, object]],
) -> None

async def list(
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[SessionRecord]

async def delete(id: str) -> None

async def create_task(
    session_id: str,
    input: CreateTaskInput,
) -> Task

async def get_task(
    session_id: str,
    task_id: str,
) -> Task | None

async def list_tasks(session_id: str) -> list[Task]

async def update_task(
    session_id: str,
    task_id: str,
    patch: TaskPatch,
) -> Task | None

async def delete_task(
    session_id: str,
    task_id: str,
) -> bool

async def claim_task(
    session_id: str,
    task_id: str,
    owner: str,
) -> Task | None

async def ready_tasks(session_id: str) -> list[Task]

async def release_task(
    session_id: str,
    task_id: str,
) -> Task | None

async def close() -> None
~~~

The required session operations have these semantics:

- `create` generates a UUID when `id` is omitted. An explicit ID is idempotent:
  an existing record is returned unchanged and its new `meta` is ignored. A new
  record starts with empty `invoked_skills` and empty message, task, and inbox
  state.
- `load` returns a detached record or `None`. `load_messages` returns detached rows
  in ascending sequence order.
- `append_messages` assigns consecutive per-session sequences in input order and
  returns detached rows in that order. All messages are encoded before
  mutation; a non-empty batch for a missing session raises KeyError, and an
  encoding or storage failure appends none of the batch. An empty batch returns
  [] without allocating a sequence; the PostgreSQL adapter returns it before
  checking session existence, while the in-memory adapter still checks the
  session.
- `update_meta` merges the supplied keys into the existing top-level metadata
  mapping and returns a detached record. It does not replace unspecified keys.
  A missing session raises KeyError.
- `set_invoked_skills` replaces the entire list with a detached copy. A missing
  session raises KeyError.
- `list` orders by descending `updated_at`, so the newest-updated session is first.
  `offset` skips that many newest rows; `limit=None` means no maximum. Returned
  records are detached.
- `delete` is an idempotent no-op for an unknown ID. It removes the session,
  messages, provider snapshot, task graph and counter, and inbox state in the
  same backend operation.
- `close` releases store-owned resources. The in-memory implementation has no
  resource to release.

Session creation, metadata/skill changes, message changes, and task changes
update the session's updated_at. Provider snapshots and inbox enqueue have
their own persistence semantics. A custom store must preserve per-session
message ordering and the detached-input/return property.

### Task values and methods

The task input value has this shape:

~~~text
CreateTaskInput(
    subject: str,
    description: str,
    active_form: str | None = None,
    metadata: dict[str, object] | None = None,
)
~~~

A returned Task has:

~~~text
id: str
session_id: str
subject: str
description: str
active_form: str | None
status: "pending" | "in_progress" | "completed"
owner: str | None
blocks: list[str]
blocked_by: list[str]
metadata: dict[str, object]
created_at: str
updated_at: str
~~~

New tasks are `pending`, unowned, and receive monotonically increasing numeric
IDs local to their session. `create_task` requires the session and raises
`KeyError` when it is missing. PostgreSQL allocates IDs with a transactional
per-session counter; the in-memory implementation is event-loop atomic.
`list_tasks` orders
by numeric task ID. All task records and nested lists and metadata returned by
the store are detached.

`TaskPatch` has these fields:

~~~text
subject: str | None = None
description: str | None = None
active_form: str | None = None
status: "pending" | "in_progress" | "completed" | "deleted" | None = None
owner: str | None = None
add_blocks: list[str] | None = None
add_blocked_by: list[str] | None = None
remove_blocks: list[str] | None = None
remove_blocked_by: list[str] | None = None
metadata: dict[str, object] | None = None
~~~

`None` in a scalar patch field means leave that field unchanged. Metadata is
merged by key; a metadata value of `None` removes that key. `owner=None` also
means leave the owner unchanged; use `release_task` to clear ownership.
`status="deleted"` deletes the task and makes `update_task` return `None`. An
absent task makes `get_task` and `update_task` return `None`.

`blocks` and `blocked_by` are reciprocal dependency edges. Adding or removing an
edge updates both endpoint lists when the other endpoint exists; references to
a future task ID are retained. Duplicate adds and removes are idempotent.
Removals are applied before additions, so an ID present in both lists is present
after the patch. `delete_task` removes the task and all references to it,
returning `True` only when a task was deleted.

`claim_task` atomically succeeds only for a pending, unowned task. It records the
supplied owner and returns the detached task; it does not itself change the
status to `in_progress`. If another owner wins, or the task is not pending or is
already owned, it returns `None`.

`ready_tasks` returns pending, unowned tasks in numeric ID order when every ID in
`blocked_by` exists and has status `completed`. A missing dependency makes the
task not ready. `release_task` returns `None` for a missing task, returns a completed
task unchanged, and otherwise clears its owner and resets its status to
`pending`. Concurrent PostgreSQL claims have one winner.

### Optional provider-view snapshots

A store may implement this optional capability:

~~~text
async def save_provider_snapshot(
    id: str,
    snapshot: ProviderViewSnapshot,
) -> None

async def load_provider_snapshot(
    id: str,
) -> ProviderViewSnapshot | None
~~~

`ProviderViewSnapshot` contains `provider_view: list[Message]`,
`covers_seq: int`, and optional `projection_id: str | None = None`.
`covers_seq` is the highest message sequence represented by the view, not a
message count. Its wire schema is version `3`.
Saving upserts one compacted provider view and its message watermark; it never
changes or removes append-only messages. Loading returns None when no snapshot
exists.

Session reload treats this as a cache. If the loader is absent, the full
message history is used. If a snapshot loads successfully and its `covers_seq` is
between zero and the highest stored message sequence, the snapshot view is
followed by messages with greater sequences. An out-of-range watermark or
non-monotonic loaded message sequence falls back to full history. An ordinary
loader error also falls back; an unsupported wire version propagates. The
saver is best-effort for ordinary failures. The loader and saver are detected
independently: a loader can restore an existing snapshot without a saver, and
a saver can persist future snapshots without a loader.

### Optional durable inbox

`SessionInboxStore` adds exactly-once insertion for messages destined for a later
turn:

~~~text
async def enqueue_inbox(
    id: str,
    delivery: InboxDelivery,
) -> None

async def commit_inbox(
    id: str,
    *,
    after_seq: int,
) -> list[StoredMessage]
~~~

An `InboxDelivery` contains a non-empty `delivery_id`, a user-role message,
non-empty `source` defaulting to `"host"`, JSON-compatible `metadata`, and a digest
computed from those values. `enqueue_inbox` requires an existing session.
Repeating the same delivery ID with the same digest is an idempotent no-op;
using that ID with another digest raises ConfigError.

`commit_inbox` requires a non-negative integer `after_seq`; `bool` is not a valid
integer here. It examines pending deliveries in FIFO enqueue order, decodes
every pending message before inserting any, and atomically appends all of them
to the session message sequence. A decode or storage failure leaves the whole
pending batch uncommitted. The result contains every stored message with
`seq > after_seq`, including messages committed by an earlier call and directly
appended messages, so a lost response can be retried safely. Results are
detached. After a successful commit the receipt and digest remain, while the
built-in stores clear the durable payload/source/metadata fields.

Unknown sessions raise KeyError for both inbox methods. Corrupt stored payloads
raise the decoder's ConfigError, json.JSONDecodeError, KeyError, TypeError, or
ValueError as appropriate.

When `DurabilityOptions.durable_inbox` is true, both `enqueue_inbox` and
`commit_inbox` are required; a missing method raises `ConfigError` before
delivery execution. The option defaults to `false`.

### Optional explicit session reservation

Both built-in session stores also expose this convenience method, which is not
required by the `SessionStore` protocol:

~~~text
async def create_if_absent(
    *,
    id: str,
    meta: dict[str, object] | None = None,
) -> SessionRecord | None
~~~

It returns the newly created detached record, or `None` when the ID already
exists. PostgreSQL uses one `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`
decision; the in-memory operation has no suspension point. This capability is
used when a caller must reserve a fork destination before copying history.

## Atomic phase writes

Persist the checkpoint that describes a phase before performing the external
operation represented by that phase. The durable loop writes an initial
`started` checkpoint with `create_run(initial_checkpoint=...)` before it begins
provider or tool work. A pending provider call, permission decision, or tool
batch must have a checkpoint before its external side effect.

Use `save_checkpoint_with_events` when a phase also produces persisted events. It
commits the checkpoint and event prefix together, assigns event sequences from
the same per-run counter, and fences both parts with one lease check.
`mark_failed` provides the corresponding atomic failed status, optional
checkpoint, error metadata, and terminal event batch. `mark_completed` stores the
final completed checkpoint and status. If serialization or storage fails, the
operation leaves the previous durable boundary intact.

Session `append_messages` and inbox `commit_inbox` have the same all-or-nothing
requirement. The optional run `append_events` capability is all-or-nothing when
implemented; if it is absent, a fallback that calls `append_event` repeatedly
can partially append before a later call fails. Validate and encode every input
before mutating state; in PostgreSQL, allocate the sequence counter and insert
the rows in one transaction or data-modifying statement.

## Optional leases and fencing

Both built-in run stores implement the optional lease capability. Its exact
methods are:

~~~text
async def claim(
    run_id: str,
    owner: str,
    ttl_s: float,
) -> RunLease

async def renew(lease: RunLease) -> RunLease

async def release(lease: RunLease) -> None
~~~

`RunLease` is immutable and contains `run_id`, `owner`, `epoch`, `expires_at`
(a Unix timestamp), and `ttl_s`. `ttl_s` must be positive and finite. `claim`
requires an existing run; an unknown ID raises `KeyError`. Another owner
holding a live lease raises `RunLeaseConflictError`. A live same-owner claim
refreshes the expiry without changing the epoch. A claim after expiry or after
release advances the epoch.

`renew` requires the exact current owner and epoch and a lease that has not
expired. It retains the epoch and uses the token's ttl_s; a stale, expired, or
otherwise mismatched token raises `LeaseLostError`. Expiry alone does not fence a
matching write or release: until another claim changes the epoch, the owner
and epoch still match. `release` clears ownership and advances the epoch, so the
released token can never be used again. A stale release changes nothing.

Every run mutation that accepts `lease`, including checkpoint writes, event
batches, terminal writes, pending-run adoption, and model-input snapshot
mutations, validates the owner and epoch as part of that operation. Once a
different owner claims the run or a matching lease is released, all writes
using the old token raise `LeaseLostError` and leave state unchanged. A mutation
without a token remains an unleased mutation and can proceed even while another
token is live; a store cannot infer ownership from another store instance's
lease. Hosts must propagate each active token on every mutation when they need
ownership protection.

~~~python
from linch import RunLeaseConflictError

async def handle(checkpoint):
    run = await runs.create_run("session-123", id="run-456")
    try:
        lease = await runs.claim(run.id, owner="worker-unique-id", ttl_s=30.0)
    except RunLeaseConflictError:
        return
    try:
        lease = await runs.renew(lease)
        await runs.save_checkpoint(run.id, checkpoint, lease=lease)
    finally:
        await runs.release(lease)
~~~

The host schedules heartbeats, stops work after lease loss, and releases the
lease only after the run has quiesced. The agent loop does not start a
heartbeat. External tools still need their own idempotency or fencing because
a run-store lease cannot roll back an already completed external effect.

## Failure semantics

Apart from the bounded admission retry described above, run-store and
session-store writes are not silently retried by the store.
Encoding failures, validation failures, and stale-lease failures are
fail-closed: the target durable state remains unchanged. A transient database
failure during an active durable run interrupts the attempt before terminal
persistence, preserving the last durable checkpoint for retry or takeover. If
`create_run` fails, that store exception propagates before the loop starts; no
terminal run events are emitted for a run that was never created.

Resuming a terminal run, or a run whose status has no resumable checkpoint,
raises `RunNotResumableError` with the run ID and status and performs no
provider, tool, session, or terminal store writes. A missing run, a different
session owner, an incompatible wire version, or an incompatible run contract
is rejected before provider/tool execution.

Provider-view snapshots are a cache: ordinary load errors rebuild the view from
the append-only session history, and ordinary save errors are logged without
changing the conversation. Exact model-input snapshot writes are durability
operations; invalid requests and integrity failures do not create a snapshot.
Deletion and pruning of completed model-input snapshots are best-effort cleanup:
ordinary failures are logged, while `LeaseLostError` propagates. Cleanup is
skipped after lease loss.

## Reconnect using the persisted event sequence

Each persisted run event has a monotonically increasing, 1-based sequence local
to its run. Single-event appends, batch appends, checkpoint-plus-event writes,
and failed terminal writes share the same counter. Read after an exclusive
cursor and advance the cursor only after the client has accepted the row:

~~~python
cursor = last_delivered_seq  # 0 for a new connection
for row in await runs.load_events(run_id, after_seq=cursor):
    await publish({"seq": row.seq, "event": row.event})
    cursor = row.seq
~~~

Rows are returned in ascending sequence order. Persist the cursor after
delivery; when acknowledgment is uncertain, deduplicate by (run_id, seq).
Notifications or queue wakeups are only hints—the ordered event log and its
cursor are the recovery source.

## Backend conformance

The storage tests exercise detached values, checkpoint/event atomicity,
sequence allocation, terminal writes, reconnect cursors, schema checks,
provider snapshots, durable inbox behavior, task dependencies, and lease
fencing. Postgres tests require asyncpg and AGENT_KIT_TEST_PG_DSN; without that
environment variable they skip the live database portion.

~~~bash
AGENT_KIT_TEST_PG_DSN=postgresql://user:password@localhost/linch_test \
  PYTHONPATH=src \
  python -m pytest \
  tests/storage/test_run_store_conformance.py \
  tests/storage/test_run_store_leases.py \
  tests/storage/test_sessions.py \
  tests/storage/test_model_input_snapshots.py \
  tests/storage/test_durable_inbox.py \
  tests/storage/test_provider_snapshot.py -q
~~~
