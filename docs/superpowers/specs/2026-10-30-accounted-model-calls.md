# Accounted SDK model calls: consolidated authority

Status: consolidated design authority for the Linch 3.0 Release-A accounting
slice. This document removes superseded proposal branches. It is a contract
for implementation and tests; it is not permission to change prompts, persona,
provider wire contracts, or public behavior outside the stated additions.

Target: Python >=3.10, dependency-free accounting correctness.

## 1. Non-negotiable behavior

1. A physical delivery that reaches normalized message_end with a valid Usage
   settles that delivery exactly once, even when its output is empty,
   non-text, rejected, or contains tool use. Usage and actual-model cost are
   independent of output usability.
2. Missing final usage is unknown, never zero. Cancellation, transport failure,
   or early generator close after dispatch and before valid message_end records
   unknown usage. A pre-dispatch cancellation, admission denial, or abort is
   not billed use and has usage_status=not_dispatched.
3. Every physical provider.stream invocation gets a fresh
   delivery_attempt_id. Retries, fallbacks, and frozen-request replays retain
   logical_call_id only when they are the same intended operation. Deduplicate
   local accounting by delivery ID, never by logical ID.
4. Auxiliary calls use the run AbortContext and exactly one provider-slot /
   limiter acquisition per physical stream. They do not call
   _SpanLifecycle.start_provider_call or end_active_provider_call; those APIs
   own the primary turn's singleton active span.
5. A successful auxiliary result is not complete until its bounded receipt is
   durably persisted. An accepted receipt stores bounded accepted summary text
   and its digest, so recovery can apply the paid result without another model
   call. A rejected receipt is terminal for that logical attempt; a later
   retry gets a new delivery ID.
6. Valid observed usage is copied and retained in memory immediately, including
   when receipt persistence later fails. Persistence failure terminates the
   run's continuation: no summary is accepted, no hook/append/dispatch follows,
   and the run does not claim a durable successful settlement. A durable
   intent without a receipt is unresolved after process loss, never free or
   fabricated zero.
7. In an uncapped run, totals may be a known lower bound and
   usage_complete=false. If a RunBudget token or USD limit is configured and a
   dispatched call has unknown usage, block further model calls with a
   budget-accounting error. Continuing would falsely claim cap enforcement.
8. Existing primary UsageEvent ordering and payload shape remain compatible.
   Optional source/call/model fields are additive and may be omitted from
   legacy primary events when an exact-shape compatibility test requires it.
   No normal primary request, prompt, system block, or exact primary input
   snapshot changes.

## 2. Types and public journal surface

All model-call records are immutable slotted dataclasses. Usage is copied on
admission/settlement so caller or hook mutation cannot change a receipt.

~~~python
ModelCallKind = Literal["primary", "compaction"]
UsageStatus = Literal["reported", "unknown", "not_dispatched"]
CallDisposition = Literal[
    "accepted", "rejected", "rejected_native", "cancelled",
    "failed", "not_dispatched",
]

@dataclass(frozen=True, slots=True)
class ModelCallIntent:
    logical_call_id: str
    delivery_attempt_id: str
    parent_logical_call_id: str | None
    kind: ModelCallKind
    run_id: str
    turn_index: int | None
    requested_model: str
    request_fingerprint: str
    strategy_id: str | None

@dataclass(frozen=True, slots=True)
class ModelCallReceipt:
    logical_call_id: str
    delivery_attempt_id: str
    kind: ModelCallKind
    run_id: str
    turn_index: int | None
    actual_model: str
    usage_status: UsageStatus
    usage: Usage | None
    cost_usd: float | None
    disposition: CallDisposition
    rejection_reason: str | None
    accepted_text: str | None
    accepted_text_sha256: str | None
    duration_ms: int
    request_fingerprint: str
    strategy_id: str | None
    source_context_fingerprint: str | None
~~~

request_fingerprint is required for every call; strategy_id may be None for primary calls. accepted_text is only
bounded auxiliary compaction text in Release A. Its first-stage UTF-8 bound is
1 MiB; an oversized response is rejected while valid final Usage is settled.

~~~python
@dataclass(frozen=True, slots=True)
class AuxiliaryTextResult:
    receipt: ModelCallReceipt
    text: str | None
~~~

The private ledger lives in src/linch/model_calls.py:

~~~python
class _ModelCallLedger:
    def __init__(
        self, run_id: str, *, budget: RunBudget | None = None,
        persist: Callable[[ModelCallEvent], Awaitable[None]] | None = None,
    ) -> None: ...
    async def begin(self, intent: ModelCallIntent) -> None: ...
    async def settle(self, receipt: ModelCallReceipt) -> bool: ...
    async def abandon(self, intent: ModelCallIntent, *, reason: str) -> None: ...
    async def restore(
        self, state: Mapping[str, Any] | None,
        journal: Sequence[ModelCallEvent],
    ) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def assert_may_start(self) -> None: ...
    # total_usage, total_cost_usd, usage_complete are detached read-only
    # properties.

def intent_to_dict(intent: ModelCallIntent) -> dict[str, Any]: ...
def intent_from_dict(raw: Mapping[str, Any]) -> ModelCallIntent: ...
def receipt_to_dict(receipt: ModelCallReceipt) -> dict[str, Any]: ...
def receipt_from_dict(raw: Mapping[str, Any]) -> ModelCallReceipt: ...
~~~

settle returns false for an already-settled delivery_attempt_id. It is the
only operation that changes cumulative usage/cost or calls the budget receipt
helper. Actual model is recorded before cost calculation. Unknown-model
pricing counts tokens, leaves cost_usd=None for that line, and keeps the
known-cost aggregate partial rather than fabricating a price.

Add typed public ModelCallEvent and is_model_call_event to linch.__all__.
ModelCallEvent is a versioned journal envelope containing run_id, record_kind,
and a detached JSON-safe record. Journal record kinds are intent,
dispatch_started, receipt, abandoned, and projection. The decoder uses the
strict intent/receipt codecs. Unknown required versions, invalid usage or
disposition, mismatched intent/receipt identity, conflicting duplicate
receipts, and corrupt hashes fail closed.

Ordinary primary delivery records are not additionally yielded on the public
event stream. Auxiliary UsageEvent records are additive and carry:

~~~text
source="compaction"
call_id=<delivery_attempt_id>
logical_call_id=<logical_call_id>
model=<actual_model>
~~~

## 3. Namespaces, checkpoint state, and durability

Accounting uses exactly checkpoint.extension_state["linch.model_calls.v1"].
That namespace has its own schema version and must be merged, not replace
other extension namespaces. Reliability's canonical namespace is exactly
linch.reliability, with a version inside its value; it is not
linch.reliability.v1. Only the runner merges model_calls, reliability, and
tool-surface namespaces.

The namespace value is a runtime envelope with version=1, ledger (the private
ledger snapshot below), and active_primary (the versioned active attempt
binding or null). Runner/checkpoint helpers own this envelope; the ledger owns
its nested snapshot. Do not pass unrelated runtime keys into ledger.restore.
The model-call ledger snapshot contains:

~~~json
{
  "version": 1,
  "budget_owner_id": "...",
  "settled_delivery_ids": ["..."],
  "receipts": ["... ModelCallReceipt ..."],
  "total_usage": {"...": "..."},
  "known_cost_usd": 0.0,
  "has_unknown_usage": false,
  "pending_intents": ["... ModelCallIntent ..."],
  "consumed_auxiliary_receipt_ids": ["..."]
}
~~~

known_cost_usd is 0.0 or null; it is not a fabricated estimate.

settled_delivery_ids is compact/rollover-capable. The durable receipt journal
is authoritative when newer than a checkpoint: reconcile journal receipts by
delivery_attempt_id and derive totals; never reprice aggregate usage at resume.
The checkpoint is a fast snapshot, not a second source of charges.

Use event-first durability. Baseline stores must not lose receipts because
event append and checkpoint save are not atomic. Reconcile a journal-ahead
checkpoint. An atomic append-events-plus-checkpoint capability may be used
opportunistically; it is not required for the baseline. SQLite is the natural
place for such a later transaction, but it is not required to start Release A.

An accepted, unconsumed auxiliary receipt is applied once on resume and then
marked consumed. An unconsumed rejected receipt finishes its compaction intent
without replay. A pending or abandoned unknown attempt is not a receipt. A
new attempt is allowed only when the run is uncapped or the host explicitly
resolves the unknown-accounting fence.

The RunContract fingerprints accounting codec version, receipt semantics,
model-source handling, accepted-text bound, and selected replay behavior.
An identical delivery ID with any conflicting immutable receipt payload fails
closed.

Reliability stores only a reference to the common receipt. A received rejected
primary outcome stores sanitized disposition, reason, and turn sufficient to
reconstruct its fixed diagnostic and pending feedback without reinference; it
does not persist rejected raw arguments. Pending-message reconciliation uses
ordered durable prefixes before frozen replay.

## 4. Durable dispatch and settlement state machine

For every physical dispatch:

1. Mint a fresh opaque delivery_attempt_id and persist ModelCallIntent.
2. Acquire exactly one limiter/provider-slot permit.
3. Check cancellation and admission, including ledger.assert_may_start().
4. Persist dispatch_started. If this marker cannot be persisted, there is no
   provider dispatch.
5. Synchronously recheck cancellation and admission.
6. Only now advance the provider iterator / call provider.stream.
7. Normalize the response, validate its event sequence, capture Usage at
   message_end, and classify the output.
8. Copy valid Usage and record observed spend in memory immediately.
9. Persist the receipt before successful continuation, projection, or another
   dispatch. Receipt settlement is durable only after this succeeds.
10. Settle before AfterProviderCall control, accepted assistant append, or any
    other model dispatch.
11. Release stream resources and the limiter in finally.

Intent-only recovery is not_dispatched and does not poison a shared budget.
dispatch_started without a receipt is conservatively unknown, including a
crash after the marker but before the network request. A new retry therefore
must pass the unknown-use fence.

The normalized provider contract requires a valid message_start.model.
Accounting preserves this strictness and never falls back to the requested
model when it is absent or invalid. A malformed native JSON/non-object
argument is retained as a private failure fact while a structurally valid
stream is drained through message_end; reported Usage is preserved, and the
whole batch is rejected before tool execution.

An output is accepted only when it is nonblank, text-only, and contains no
tool use. Empty, non-text, tool-using, oversized, or otherwise unusable
compaction output is rejected, but valid received Usage is still charged.
Pre-end cancellation/transport/generator failure after dispatch is
cancelled/failed or abandoned with usage_status=unknown.

## 5. Auxiliary gateway and compaction integration

Add private _AccountedAuxiliaryCaller in model_calls.py and inject it into
CompactionContext. Its public-to-compaction API is:

~~~python
class _AccountedAuxiliaryCaller:
    def __init__(
        self, provider: Any, ledger: _ModelCallLedger, *,
        limiter: Any = None, parent_logical_call_id: str | None = None,
        turn_index: int | None = None,
    ) -> None: ...
    async def text(
        self, *, logical_call_id: str, model: str,
        system: list[SystemBlock], messages: list[Message],
        max_output_tokens: int, signal: AbortContext,
    ) -> AuxiliaryTextResult: ...
~~~

The caller persists intent, admits the call, acquires one provider slot,
streams with the same AbortContext signal, validates normalized events,
captures message_end Usage, classifies output, persists/flushed receipt and
bounded accepted text, and releases the stream/limiter in finally. It does not
open or close the primary lifecycle span.

DefaultCompaction and DetailedCompaction use this caller rather than directly
consuming provider.stream. summarize_with_provider may remain a thin
compatibility wrapper that returns result.text only after checking accepted
disposition. Remove the existing outer strategy-level provider_slot so a
limiter of one cannot deadlock or double-acquire.

Preserve the public custom CompactionStrategy protocol. Supply an accounted
facade for the Linch provider: compliant custom strategies receive accounting,
cancellation, and limiter guarantees through that facade. Do not blanket
reject existing strategies. Opaque network calls outside the supplied facade
or gateway are not automatically observable and are outside the SDK
accounting guarantee. Custom strategies do not automatically receive paid
final-projection replay; that guarantee is only for built-ins or an explicit
deterministic result/commit seam.

maybe_compact_resilient creates one logical ID for one retry sequence and
passes it to each delivery. After compaction returns, the runner drains
queued auxiliary events, saves/reconciles accounting state, and immediately
rechecks ledger admission before constructing or dispatching the primary
request. A summary that crosses a budget cannot be followed by an uncharged
main turn.

Compaction receives no exact primary ModelInputSnapshot. Exact snapshots remain
primary-request durability. Auxiliary intent/receipt metadata is the semantic
audit record and never duplicates the full user request.

Preserve the configured compaction strategy, prompt, output cap,
full_history/provider_view semantics, and existing persona. Do not add a
native module loader, model-specific billing adapter, third-party dependency,
or prompt rewrite.

## 6. Primary accounting, replay, and reliability

The run-scoped ledger includes primary calls. Replace the current small
primary post-assembly accounting block with one ledger settlement call, then
emit the existing UsageEvent using ledger totals. Existing primary lifecycle
hooks and primary event order remain intact where compatible.

The checkpoint owner binds logical_call_id, delivery_attempt_id, turn index,
snapshot identity, and final-request fingerprint for the active primary
attempt. Frozen replay reconciles against that exact attempt before dispatch:

- A reported rejected_native receipt reconstructs the fixed safe reliability
  diagnostic/pending sequence by receipt reference; there is no reinference.
- A reported accepted receipt with matching durable assistant at its recorded
  base boundary/digest continues from that assistant without reinference.
- A reported accepted receipt with no durable assistant and unchanged durable
  base permits a NEW charged delivery under the SAME logical operation, after
  budget/cancellation admission. It never reuses the old delivery ID.
- A started attempt without a receipt applies the unknown-use fence before
  another attempt.
- An intent-only attempt provides no billed-use inference; a real retry gets a
  fresh delivery ID.
- A wrong snapshot, unexpected message suffix, or conflicting receipt identity
  fails closed rather than matching a different provider attempt.

A receipt-linked durable primary binding stores the durable message base
boundary and post-hook normalized assistant digest where needed, not rejected
raw arguments. The immutable ModelCallReceipt fields remain unchanged; see the
primary-append binding below. A rejected diagnostic plus
feedback has an expected receipt-keyed two-message sequence: durable prefix
states 0/1/2 append only the missing suffix; any mismatched durable suffix is
an error.

## 7. Paid projections and recovery

Built-in DefaultCompaction and DetailedCompaction record stable projection_id,
source receipt IDs, strategy identity, and source/target view fingerprints in
the accounting journal and optional provider-view snapshot. The projection ID
must remain in the optional snapshot; checkpoint consumption lists alone are
insufficient.

Before applying a stored paid result, the current provider-view fingerprint
must match the recorded source. If it already matches the target, application
is a no-op. A different context fails the replay rather than applying a stale
summary. A lost provider-view cache may be rebuilt from durable source plus
the paid response, then applied as the same deterministic replacement without
inference, another delivery, duplicate summary text, or another charge.

Test recovery in both projection-before-consumption and
consumption-before-projection order across the separate stores.

Ordinary custom strategies using the supplied provider facade receive
accounting/cancellation/limiter guarantees only. Multiple provider calls or
arbitrary transforms cannot be mapped back from one text digest, so a resumed
custom strategy without an explicit deterministic result/commit seam gets a
new operation/delivery if it must run again; prior reported usage remains
counted. Undeclared external effects remain outside SDK guarantees.

Full activation remains in scope and does not depend on completing new
raw-artifact or token-compaction features when existing immutable full-history
and common control-field projection invariants are proved.

## 8. Shared RunBudget and ownership fences

Keep public RunBudget.charge unchanged. Add only private receipt-aware state
and:

~~~python
def _charge_receipt(
    self, delivery_attempt_id: str, usage: Usage,
    cost_usd: float | None,
) -> bool: ...
~~~

The private state contains budget_owner_id and a charged-delivery-ID set. The
ledger calls _charge_receipt, never charge directly. A same live RunBudget
object shares owner and IDs and replays no historical charge.

A fresh zero-spent RunBudget (zero counters, no owner, no charged IDs) may
adopt the checkpoint owner and replay that run's settled receipts once. A
fresh nonzero or owner-mismatched capped budget fails closed with ConfigError:
the host must reuse/restore the owner budget or supply a fresh zero-spent
budget. The private restore handshake returns same_owner or fresh_adopted.
An owner-matched budget must already contain every restored charged ID with
consistent counters; it is not silently filled in. Extra IDs from live
siblings sharing that owner are valid.

Unknown dispatched use is registered with the same live RunBudget owner, so a
parent or sibling sharing the budget cannot continue a capped run merely
because its own ledger lacks the child's unknown receipt. Owner restore checks
this fence as well as charged IDs.

This is an in-process shared-tree guarantee only. It does not claim
distributed-tree cap recovery, exactly-once external-provider billing, or a
reservation against concurrently admitted deliveries. Admission retains
observed-spend semantics; these limits must be documented and tested.

## 9. Persistence and implementation surface

The intended files are:

- src/linch/model_calls.py: private intent/receipt/ledger/gateway,
  codecs and journal reconciliation.
- src/linch/compaction.py: injected caller, no outer limiter, unchanged
  summary prompt and strategy semantics.
- src/linch/loop/runner.py: construct ledger, replace primary accounting
  block, inject caller, drain auxiliary events, merge checkpoint state, and
  recheck admission after compaction.
- src/linch/events.py and run_store.py: additive receipt journal codec/event
  and optional atomic-store capability.
- src/linch/budget.py: private owner/deduplication helper only.
- src/linch/session.py / checkpoint codec: merge the accounting namespace
  without replacing reliability or checkpointable-hook namespaces.

No other public API expansion is required beyond public ModelCallEvent,
is_model_call_event, and optional additive UsageEvent fields. Existing
primary-only event/hook behavior is preserved when accounting metadata is
absent.

## 10. Acceptance tests

1. Normal compaction with nonzero usage increments cumulative Usage,
   ResultEvent, checkpoint total, priced cost, and one shared budget exactly
   once; the primary call afterward sees the new total.
2. Empty-text, tool-use, oversized, or otherwise rejected compaction with
   message_end Usage is charged once and journaled as rejected; it cannot
   disappear from totals or budget.
3. Cancellation/error before message_end releases provider_slot, records
   unknown usage, preserves primary span state, and blocks later model calls
   when a cap is configured. A journal write failure retains observed spend
   but permits no successful continuation.
4. Retry/fallback produces one logical ID, distinct delivery IDs, actual
   models/costs, and one charged receipt per delivery that reached
   message_end.
5. A limiter-of-one compaction completes without nested acquisition deadlock;
   main and auxiliary cancellation leave its permit count intact.
6. Crash simulation at intent, dispatch_started, receipt, projection,
   checkpoint, and event-log boundaries: resume does not re-charge a settled
   delivery; an accepted receipt applies its stored summary without a new
   provider call; an unresolved started attempt remains fenced.
7. Compaction crossing max tokens/USD prevents the next primary dispatch and
   emits the normal budget terminal path.
8. Same-object shared-budget resume does not replay charges; fresh zero budget
   restores spend; fresh nonzero/mismatched capped budget fails closed.
9. Existing primary-only event/hook tests retain order and payload behavior
   absent new accounting metadata; prompt/persona and exact primary input
   snapshot remain unchanged.

Authoritative offline regression: summary Usage(111 input, 22 output) is
followed by main Usage(333 input, 44 output). The old behavior accounts only
377 main tokens rather than 510 total. With max_tokens=100, the 133-token
summary exhausts the cap, so the main call must not dispatch. The uncapped
case must report all 510 tokens. This is behavioral evidence, not a claim
about model quality.

## 11. Deliberately unspecified internals

The contract does not prescribe the concrete journal backend/encoding,
identifier generator, exact RunStore transaction implementation, provider-view
snapshot wire layout, or fingerprint/hash implementation beyond the required
accepted-text SHA-256 and strict codec/hash validation. Those details must fit
existing Linch interfaces and the tests above; they must not add policy or
weaken any ordering, identity, bound, or recovery rule in this document.

## 12. Implementation bindings

These private bindings make task boundaries concrete without adding public API.

- ModelCallEvent is defined in events.py as a slotted envelope with run_id,
  record_kind (intent/dispatch_started/receipt/abandoned/projection), record
  (detached JSON object), budget_owner_id (optional string), schema_version=1,
  and type=model_call. The strict model-call codecs validate its record.
- model_calls.py owns _ModelCallError(RuntimeError) with a bounded code, the
  intent/receipt codecs, ledger and auxiliary gateway. Event decoding may
  lazy-import the private codecs to avoid an import cycle.
- Add async _ModelCallLedger.mark_dispatch_started(intent). begin is intent-only;
  settle requires the matching started intent. abandon records not_dispatched
  for an intent without dispatch_started, otherwise unknown and a shared-owner
  fence. It never invents an actual reported model when no message_start arrived.
- Add RunBudget private _charge_receipt(delivery_attempt_id, usage, cost_usd),
  _restore_receipts(budget_owner_id, receipts), and unresolved-delivery tracking.
  Public charge() semantics/signature remain unchanged.
- Codec dictionaries are independently versioned at1; invalid required fields,
  booleans masquerading as token integers, negative/nonfinite usage/cost,
  duplicate/conflicting delivery records, wrong hashes and unknown required
  versions fail. Ingress/egress and persisted envelope records are detached.
- request_fingerprint is required for every actual call; strategy_id may be None
  for primary calls. Physical IDs are fresh opaque UUIDs, never reconstructed
  from turn/checkpoint counters.
- Receipt journal durability applies when a RunStore is configured. Without
  durable runs, the same state machine commits to a run-local in-memory journal;
  accounting/admission still work but process-restart recovery is not claimed.
- ResultEvent adds usage_complete: bool=True, omitted from the legacy wire shape
  when true and round-tripped when false. RunReport propagates that flag.
  Unknown usage makes totals an explicit lower bound. Existing partial-cost
  semantics remain documented; no new complete-cost claim is introduced.
- Ordinary custom strategies receive accounting through Linch's provider facade.
  Paid projection replay is guaranteed only for built-ins or an explicit custom
  commit seam; the generic facade must not claim it can reconstruct arbitrary
  transformations.
- The current provider contract requires valid message_start.model. There is no
  requested-model fallback. Malformed native arguments must drain a structurally
  valid message_end and preserve reported usage before whole-batch rejection,
  using a shared private failure fact produced by accounting stream assembly.
  Accounting initially terminates that rejected batch; reliability later owns
  bounded correction/recovery and consumes the same receipt.

## Cross-session primary parser integration ruling

The first accounting release handles malformed native JSON/nonobject arguments
with sanitized rejected_native + terminal_error_v0, after draining a structurally
valid message_end and settling its reported Usage once. Reliability later uses
the same receipt with bounded_feedback_v1; RunContract fingerprints these
distinct semantics so a terminal run cannot silently resume with correction.

For stream_partials=True, stage durable partial events per physical delivery
until whole-response validation. Discard staged events for rejected/mixed native
batches. Keep immediate live partial delivery and accepted-response event order.
Rejected raw arguments must not enter SDK session, model-call, error or partial
journals; host-owned live-stream consumers are outside SDK persistence.

## Primary acceptance binding (supersedes digest-on-receipt wording)

Before intent/dispatch, persist the active-primary checkpoint binding: logical
ID, delivery ID, turn, exact snapshot ID, final-request fingerprint, durable
full-history base count and base fingerprint. A failure to persist that binding
prevents dispatch. Intent/dispatch/receipt journal ordering is otherwise unchanged.

After settlement and AfterProviderCall mutation, but before session append,
persist a strict ModelCallEvent projection variant with
record.projection_kind=primary_append. It binds the same receipt/delivery,
logical operation, snapshot/request identity and durable base to the POST-hook
assistant digest. It is distinct from auxiliary compaction projections and
reliability diagnostic/feedback actions, and is never emitted for a rejected
native assembly. Provider Usage is copied before hooks and stays authoritative.

On resume an exact matching durable assistant at that base continues without
reinference; no assistant with unchanged base permits a NEW delivery/charge;
wrong suffix, snapshot, digest or identity fails closed. This explicitly
supersedes the earlier wording that placed the assistant digest on the receipt.
No ModelCallReceipt field expansion is required.

The first accounting prerelease uses terminal_error_v0 for rejected-native
responses, reconstructing its safe terminal error from the receipt without
inference. The later reliability feature owns bounded_feedback_v1 and its
receipt-keyed diagnostic/feedback prefix0/1/2 actions. A shared prefix checker
may be tested with fixed fixtures in accounting, but accounting does not enable
reliability correction or append its feedback.
