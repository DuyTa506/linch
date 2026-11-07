# Events

> Part of the [linch-rust architecture guide](./README.md).

A run reports everything it does as a stream of `Event`s, each in a
versioned `Envelope`. The same envelopes are the run's durable event log in
the run store, the `run.event` notifications of the RPC protocol, and what
the OpenTelemetry layer and the run reports read. The vocabulary lives in
`linch-core` (`crates/linch-core/src/events/`); the Python client decodes it
in `python/src/linch_rust/events.py`. Use this page when you add or change
an event kind, decode envelopes outside the core, or change where a kind is
stored.

Python Linch has the same idea ([event taxonomy](../../../docs/architecture/events.md)).
What differs in Rust:

- `Event` is one closed enum with a hand-written codec, and every payload
  rejects unknown fields on decode. A different wire version `v` is refused.
- Python's `SystemEvent` (`type: system`, `subtype: init`) is `init` here.
  Python's `BackgroundWorkerEvent` has no kind of its own: a worker's
  status changes are `child_run_status` events.
- Python's model-call ledger records (`intent`, `dispatch_started`,
  `abandoned`, `projection`) are three journal-only kinds:
  `model_call_delivery`, `model_call_receipt` and
  `model_call_usage_settlement` (`crates/linch-core/src/events/model_call.rs:1-9`).
- There is no lifecycle bus to subscribe to. Observers read the envelopes
  from the run's stream or the store; hooks are a separate mechanism
  (`crates/linch-core/src/hooks.rs`).

## The event enum

`Event` (`crates/linch-core/src/events/event/mod.rs:48`) has one variant per
kind. Each payload is a plain struct in `events/kinds/*.rs` or
`events/model_call.rs` that derives `Serialize` and `Deserialize` with
`deny_unknown_fields` and has no `type` field of its own; `Event` adds and
strips the tag by hand (`events/kinds/mod.rs:1-9`). `Event::type_name`
(`crates/linch-core/src/events/event/name.rs:12`) gives the tag without
encoding.

| `type` | Payload, in `events/` | Stored | Delivered | Notes |
|---|---|---|---|---|
| `init` | `InitEvent`, `kinds/lifecycle.rs:19` | yes | yes | One per call of the run: the start, and each resume or approve with `resumed` and the `turn` it continues in |
| `user` | `UserEvent`, `kinds/lifecycle.rs:62` | yes | yes | `subtype`: `prompt`, `tool_result`, `alignment`, `notification` |
| `assistant` | `AssistantEvent`, `kinds/lifecycle.rs:72` | yes | yes | |
| `partial_assistant` | `PartialAssistantEvent`, `kinds/lifecycle.rs:82` | no | yes | |
| `usage` | `UsageEvent`, `kinds/control.rs:13` | yes | yes | |
| `tool_call_start` | `ToolCallStartEvent`, `kinds/tooling.rs:11` | yes | yes | |
| `tool_call_end` | `ToolCallEndEvent`, `kinds/tooling.rs:29` | yes | yes | `tool_use_id` and `is_error` live in `result` |
| `tool_progress` | `ToolProgressEvent`, `kinds/tooling.rs:98` | no | yes | |
| `permission_request` | `PermissionRequestEvent`, `kinds/tooling.rs:127` | yes | yes | |
| `input_request` | `InputRequestEvent`, `kinds/suspend.rs:22` | yes | yes | Not emitted at this revision |
| `suspended` | `SuspendedEvent`, `kinds/suspend.rs:53` | yes | yes | Ends a suspended run's stream instead of `result` |
| `context_build` | `ContextBuildEvent`, `kinds/control.rs:44` | yes | yes | |
| `compaction` | `CompactionEvent`, `kinds/control.rs:64` | yes | yes | |
| `loop_guard` | `LoopGuardEvent`, `kinds/control.rs:105` | yes | yes | |
| `budget` | `BudgetEvent`, `kinds/control.rs:127` | yes | yes | A spec `budget` (`crates/linch-core/src/budget.rs`): `warning` right after the `usage` that crossed the ratio, once per run tree; `exceeded` in the terminal write, before its `error` |
| `prompt_cache_advisory` | `PromptCacheAdvisoryEvent`, `kinds/control.rs:168` | yes | yes | Not emitted at this revision |
| `skills_loaded` | `SkillsLoadedEvent`, `kinds/skills.rs:10` | yes | yes | |
| `skill_invoked` | `SkillInvokedEvent`, `kinds/skills.rs:18` | yes | yes | |
| `skill_completed` | `SkillCompletedEvent`, `kinds/skills.rs:35` | yes | yes | |
| `subagent` | `SubagentEvent`, `event/mod.rs:118` | as wrapped | as wrapped | See [subagent wrapping](#subagent-wrapping) |
| `child_run_status` | `ChildRunStatusEvent`, `kinds/suspend.rs:66` | yes | yes | |
| `workflow` | `WorkflowEvent`, `kinds/skills.rs:76` | yes | yes | Never in a run's log: `linch-ext` workflows hand it to their event sink ([extensions](./ext.md)) |
| `verification` | `VerificationEvent`, `kinds/control.rs:191` | yes | yes | Not emitted at this revision |
| `hook` | `HookEvent`, `kinds/hook.rs:14` | no | yes | |
| `error` | `ErrorEvent`, `kinds/lifecycle.rs:90` | yes | yes | |
| `result` | `ResultEvent`, `kinds/lifecycle.rs:115` | yes | yes | The terminal outcome |
| `model_call_delivery` | `ModelCallDeliveryEvent`, `model_call.rs:33` | yes | no | |
| `model_call_receipt` | `ModelCallReceiptEvent`, `model_call.rs:72` | yes | no | |
| `model_call_usage_settlement` | `ModelCallUsageSettlementEvent`, `model_call.rs:110` | yes | no | Not emitted at this revision |
| any other | `IgnorableEvent`, `event/ignorable.rs:51` | yes | yes | Only under an `ignorable` envelope |

"Stored" means written to the run's event log; "Delivered" means yielded by
the run's stream, and so sent to clients. The two columns are the
[persistence class](#persistence-classes).

## Envelope

`Envelope` (`crates/linch-core/src/events/envelope.rs:163`):

| Field | Meaning |
|---|---|
| `v` | Wire version; only `WIRE_VERSION`, 1, is read or written (`envelope.rs:15`) |
| `seq` | The run's sequence number. Durable events take consecutive seqs from 1; a stream-only event carries the last durable seq, so seqs on the stream repeat and have gaps |
| `run_id` | The run the event belongs to |
| `ts_ms` | The producer's wall clock in Unix milliseconds when it made the envelope (`envelope.rs:170-174`). Order by `seq`, never by `ts_ms`: a crashed run and its resume may run on clocks that disagree |
| `ignorable` | The producer's promise that an unknown `event.type` may be skipped. Absent on the wire when false |
| `event` | The event |

The run's journal makes every envelope (`crates/linch-core/src/journal.rs:297`):
`v` is `WIRE_VERSION`, `ts_ms` is the time the event was staged, and
`ignorable` is false. When an envelope becomes durable is the
[group commit](./stores.md#group-commit-write-counts) rule: a durable event
is held until the next barrier write, which carries it.

## Persistence classes

`Persistence` (`crates/linch-core/src/events/event/mod.rs:33`) has three
classes, and `Event::persistence` (`event/mod.rs:282`) maps each kind to
one. That match has no wildcard arm, so a new kind does not compile until it
is classified.

| Class | Kinds | Where it goes |
|---|---|---|
| `StreamOnly` | `partial_assistant`, `tool_progress`, `hook` | `Journal::stage` returns it at once with the last durable seq and never stores it (`crates/linch-core/src/journal.rs:123-130`). Tool progress goes straight to the stream's outbox (`crates/linch-core/src/runner/sink.rs:184-189`) |
| `Both` | every other known kind, and `IgnorableEvent` | Staged with the next seq, held until the next barrier write, then delivered in seq order (`journal.rs:310-316`) |
| `JournalOnly` | the three `model_call_*` kinds | Staged with the next seq and stored like `Both`, but never delivered. `Agent::events` leaves them out too (`crates/linch-core/src/agent.rs:380-396`), and so does the RPC method `run.events`, which calls it |

The journal-only records are what resume reads to settle a model call it
may already have paid for (`crates/linch-core/src/journal/reconcile.rs`).
Read them with `RunStore::events` directly. They take seqs, which is why a
client sees gaps in the durable seqs.

## Wire format and version

An envelope is one JSON object:

```json
{"v":1,"seq":7,"run_id":"r1","ts_ms":1762400000123,"event":{"type":"subagent","child_run_id":"r1:c1","tool_use_id":"c1","ts_ms":1762400000000,"event":{"tool_use_id":"c9","tool_name":"Read","message":"reading","type":"tool_progress"}}}
```

That is the output of the example below. The rules:

- **Encoding** is one pass with no JSON tree in between, so the RPC writer
  and the stores go straight from an `Envelope` to its text
  (`envelope.rs:249-270`). A payload's fields come first, then its `type`
  (`crates/linch-core/src/events/event/mod.rs:131-148`); a `subagent`
  event writes `type` first (`event/mod.rs:362-370`). Decoders must not
  depend on key order.
- **Omission:** an absent `Option`, a false flag (`resumed`, `is_error`), a
  zero count (`turn`, `attempt`, `occurrence`) and an empty string of
  `WorkflowEvent` are left out on the wire and read back as their default
  (`crates/linch-core/src/events/kinds/mod.rs:25-30`).
- **Money** fields encode only finite numbers; NaN or an infinity is an
  `EventError::EncodePayload`, never a silent `null`
  (`crates/linch-core/src/events/wire.rs:81-122`).
- **`canonical_output: null`** on a tool end is kept apart from an absent
  field (`crates/linch-core/src/events/kinds/tooling.rs:36-50`).
- **Decoding is strict.** `Envelope::from_value` (`envelope.rs:204`) refuses
  a missing field, a member it does not know, and any `v` other than
  `WIRE_VERSION` (`EventError::UnsupportedVersion`). A known `type` always
  decodes its payload strictly. A `type` the build does not know is
  `EventError::UnknownType` unless the envelope is `ignorable`, when it
  becomes an `IgnorableEvent` (`event/mod.rs:245-258`). Ignorable never
  rescues a malformed known kind.
- **Re-encoding** an envelope whose event holds an `IgnorableEvent`, at any
  depth, writes `ignorable: true` whatever the field says, so every
  envelope that encodes also decodes (`envelope.rs:156-161`, `envelope.rs:258`).

`EventError` (`envelope.rs:72`) is the one error type for every encode and
decode failure. `Envelope` implements `FromStr` for text and
`Deserialize`, both strict.

Because payloads reject unknown fields, a build that does not know a field
cannot read an envelope that has it. A new optional field with
`#[serde(default)]` keeps the envelopes already stored readable by the new
build; the reverse does not hold. The client and the server ship in one
wheel, so they agree. Nothing in the tree reads two versions, and
`WIRE_VERSION` has not moved from 1.

## Subagent wrapping

A subagent's child run is a run of its own, with its own journal and log.
Each of its events also reaches the parent's log inside a `subagent` event
(`crates/linch-core/src/events/event/mod.rs:118-129`):

| Field | Meaning |
|---|---|
| `child_run_id` | The child run, `<parent run id>:<tool_use_id>` of the call that spawned it |
| `tool_use_id` | The parent's call that runs the child: the `Subagent` call, or a `SubagentContinue` call |
| `ts_ms` | The child envelope's `ts_ms`. A background child's events reach the parent's log at the parent's next turn, so the outer `ts_ms` is later |
| `event` | The child's event |

The child's `seq` is not kept; read the child run's own log for it.

- **Persistence recurses** (`event/mod.rs:274-281`): a wrapped
  `tool_progress` is stream-only, a wrapped `model_call_receipt` journal-only.
- **A foreground child** is driven inside the call. Every envelope its
  stream yields, stream-only ones included, is wrapped and handed to the
  parent through the call's context (`crates/linch-core/src/subagents/run.rs:147-164`).
  A wrapped durable event rides the parent's next write, the call's end at
  the latest.
- **A background child** keeps only its durable envelopes
  (`crates/linch-core/src/subagents/background.rs:279-292`); the parent stages
  them at its next turn (`crates/linch-core/src/runner/inbox.rs:113-121`).
- **Depth** is bounded at 8 on decode (`MAX_SUBAGENT_DEPTH`,
  `envelope.rs:17-23`) as a guard against a hostile stream. A child cannot
  spawn, so real logs nest one level.
- The nested event inherits the outer envelope's `ignorable` flag.

[Skills and subagents](./skills-subagents.md) covers when children run.

## The Python client

`python/src/linch_rust/events.py` has one frozen dataclass per delivered
kind, with the wire name as the class variable `type`, gathered in the
`Event` union (`events.py:345`) and the `_KINDS` table (`events.py:375`).

- `decode_event` (`events.py:432`) builds the class of the `type`, passing
  only the members the class has, so a member the client does not know is
  dropped. A `subagent` event decodes its child the same way. Any other
  `type` becomes `UnknownEvent(type, raw)` (`events.py:338`).
- `decode_envelope` (`events.py:450`) reads `seq`, `run_id`, `ts_ms` and
  `event`. It reads neither `v` nor `ignorable`.
- Nested values (messages, usage, tool result blocks) stay the JSON dicts
  the server sent.
- The journal-only kinds have no class; the server never sends them.

So the two sides differ on purpose: Rust refuses what it does not know, and
the client tolerates it. A client older than its server keeps working on
`UnknownEvent`; a Rust reader does not.

### The parity test

`test_every_rust_event_kind_has_a_class_with_its_fields`
(`python/tests/test_events.py:133`) pins the Python classes to the Rust
payloads. `rust_kinds` (`test_events.py:114-130`) reads the Rust source as
text:

- the variants of `pub enum Event {` in `events/event/mod.rs`, written as
  `Name(Payload),` one per line;
- the `Serialize` arms written as `Event::Name(e) => tagged("name", e)`;
- each payload's `pub struct Payload {` body in `events/event/mod.rs` or
  `events/kinds/*.rs`, and its `pub field:` lines;
- `subagent` by hand, and none of the kinds in `JOURNAL_ONLY`
  (`test_events.py:26`).

It then asserts that the set of kinds and each kind's field names equal
those of the Python classes. It compares names only, not types or
optionality. A delivered kind whose payload lives in `model_call.rs`, or an
arm or struct written another way, fails it. It needs no server:

```sh
cd linch-rust/python && PYTHONPATH=src uv run pytest tests/test_events.py
```

## Example

This compiles and runs against `linch-core` at this revision, and prints
the envelope shown under [wire format](#wire-format-and-version):

```rust
use linch_core::events::kinds::ToolProgressEvent;
use linch_core::{
    Envelope, Event, EventError, IgnorableEvent, JsonObject, Persistence, RunId, SubagentEvent,
    WIRE_VERSION,
};

fn main() {
    // A child's stream-only progress, wrapped for its parent.
    let progress = Event::ToolProgress(ToolProgressEvent {
        tool_use_id: "c9".into(),
        tool_name: "Read".into(),
        message: "reading".into(),
        data: None,
    });
    let wrapped = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("r1:c1"),
        tool_use_id: "c1".into(),
        ts_ms: 1_762_400_000_000,
        event: Box::new(progress),
    });
    assert_eq!(wrapped.type_name(), "subagent");
    assert_eq!(wrapped.persistence(), Persistence::StreamOnly);

    // One envelope, encoded in one pass and decoded back.
    let envelope = Envelope {
        v: WIRE_VERSION,
        seq: 7,
        run_id: RunId::new("r1"),
        ts_ms: 1_762_400_000_123,
        ignorable: false,
        event: wrapped,
    };
    let text = serde_json::to_string(&envelope).unwrap();
    println!("{text}");
    assert_eq!(text.parse::<Envelope>().unwrap(), envelope);

    // Another wire version is refused, not guessed at.
    let newer = text.replacen("\"v\":1", "\"v\":2", 1);
    let refused = newer.parse::<Envelope>().unwrap_err();
    assert!(matches!(refused, EventError::UnsupportedVersion { found: 2, .. }));

    // An unknown kind decodes only when the producer marked it ignorable.
    let unknown = r#"{"v":1,"seq":8,"run_id":"r1","ts_ms":1,"event":{"type":"brand_new","x":1}}"#;
    assert!(matches!(
        unknown.parse::<Envelope>(),
        Err(EventError::UnknownType(_))
    ));
    let marked = unknown.replacen("\"event\"", "\"ignorable\":true,\"event\"", 1);
    let kept = marked.parse::<Envelope>().unwrap();
    let Event::Ignorable(sentinel) = &kept.event else {
        panic!("an ignorable envelope keeps the unknown kind");
    };
    assert_eq!(sentinel.type_name(), "brand_new");
    // It encodes back to the same fields (serde_json sorts them by key).
    let again: serde_json::Value = serde_json::to_value(&kept).unwrap();
    assert_eq!(again, serde_json::from_str::<serde_json::Value>(&marked).unwrap());

    // Ignorable never rescues a malformed known kind.
    let bad = r#"{"v":1,"seq":9,"run_id":"r1","ts_ms":1,"ignorable":true,
                  "event":{"type":"tool_progress"}}"#;
    assert!(matches!(
        bad.parse::<Envelope>(),
        Err(EventError::InvalidPayload { .. })
    ));

    // Encoding sets the flag whenever the event holds a sentinel.
    let sentinel = IgnorableEvent::new("brand_new", JsonObject::new()).unwrap();
    let carried = Envelope {
        ignorable: false,
        event: Event::Ignorable(sentinel),
        ..envelope
    };
    let text = serde_json::to_string(&carried).unwrap();
    assert!(text.contains("\"ignorable\":true"));
}
```

The client reads the same line, and is tolerant where Rust is strict. This
runs with `cd linch-rust/python && PYTHONPATH=src uv run python <file>`:

```python
import json

from linch_rust import SubagentEvent, ToolProgressEvent, UnknownEvent
from linch_rust.events import decode_envelope

line = (
    '{"v":1,"seq":7,"run_id":"r1","ts_ms":1762400000123,"event":{"type":"subagent",'
    '"child_run_id":"r1:c1","tool_use_id":"c1","ts_ms":1762400000000,"event":'
    '{"tool_use_id":"c9","tool_name":"Read","message":"reading","type":"tool_progress"}}}'
)
envelope = decode_envelope(json.loads(line))
assert isinstance(envelope.event, SubagentEvent)
assert isinstance(envelope.event.event, ToolProgressEvent)
assert (envelope.seq, envelope.event.child_run_id) == (7, "r1:c1")

# The client is tolerant: it reads neither `v` nor `ignorable`, drops
# members it does not know, and keeps an unknown kind as UnknownEvent.
newer = json.loads(line.replace('"v":1', '"v":2', 1))
newer["event"]["event"]["extra"] = True
assert isinstance(decode_envelope(newer).event.event, ToolProgressEvent)
unknown = {"v": 1, "seq": 8, "run_id": "r1", "ts_ms": 1, "event": {"type": "brand_new", "x": 1}}
assert decode_envelope(unknown).event == UnknownEvent("brand_new", {"x": 1})
print("ok")
```

## Add an event kind

Several lists name every kind. The compiler checks some of them, tests check
others, and one is checked by nothing:

| Step | Where | What catches a miss |
|---|---|---|
| 1. The payload struct: `Serialize`, `Deserialize`, `deny_unknown_fields`, no `type` field; `default` and `skip_serializing_if` on optional fields; `serialize_finite_f64` or `_option` on money | `events/kinds/<topic>.rs` (re-exported by `kinds/mod.rs`) | Review |
| 2. The variant, as `Name(NameEvent),` on one line | `Event`, `event/mod.rs:48` | The parity test reads this line |
| 3. The decode arm | `Event::decode`, `event/mod.rs:169` | Not the compiler: the match ends in `other =>`. `known_event_types_matches_decode_exhaustively` (`event/tests.rs:29`) |
| 4. The encode arm, as `tagged("name", e)` | `impl Serialize for Event`, `event/mod.rs:333` | The compiler; the parity test reads this arm |
| 5. The wire name | `Event::type_name`, `event/name.rs:12` | The compiler; `type_name_is_the_wire_type_tag` (`event/tests.rs:263`) |
| 6. The persistence class | `Event::persistence`, `event/mod.rs:282`; for `StreamOnly` or `JournalOnly`, also the sets in `persistence_classification_covers_every_kind` (`event/tests.rs:195`) | The compiler, then that test |
| 7. The tag in the known list | `KNOWN_EVENT_TYPES`, `event/ignorable.rs:8` | `known_event_types_matches_decode_exhaustively`; without it `IgnorableEvent::new` would accept the tag |
| 8. One sample | `one_of_every_kind`, `event/tests/samples.rs:14` | `persistence_classification_covers_every_kind` counts the samples |
| 9. A proptest builder, added to the `Union` | `arbitrary/builders.rs` or `arbitrary/accounting.rs`, and `arbitrary::event`, `arbitrary/mod.rs:185` | **Nothing.** A missed builder only shrinks the round-trip coverage of `every_kind_round_trips_through_decode` |
| 10. The Python class, in the `Event` union and `_KINDS`; or the tag in `JOURNAL_ONLY` for a journal-only kind | `python/src/linch_rust/events.py`, `python/tests/test_events.py:26` | The parity test |
| 11. The emission point | the runner (`crates/linch-core/src/runner/`) or a tool through its context | Review: a durable kind rides the next barrier, never past it |
| 12. Consumers that match on `Event` | `crates/linch-otel/src/observer.rs`, `crates/linch-ext/src/reports.rs`, `crates/linch-ext/src/reports/summary.rs` | Not the compiler: they end in wildcard arms |
| 13. The docs | this page's table; `docs/usage/events.md` for a delivered kind | Review |

Then run `cargo test -p linch-core events` and the parity test above.
Adding a variant is a wire change for older readers: an older Rust build
reads the new kind as `UnknownType` unless the producer marks the envelope
`ignorable`, and the core never does.

## Tests

| Test | Pins |
|---|---|
| `events/event/tests.rs` | Persistence of every kind, the known-type list against decode, round trips of one sample per kind and of 256 random events with nested subagents, wire names, the tool end's size (`the_tool_end_is_no_larger_than_the_result`, `event/tests.rs:284`) |
| `events/envelope/tests.rs` | Envelope strictness, versions, the `ignorable` rule, one-pass encoding |
| `kinds/*.rs` unit tests | Field-level rules: required result fields, omitted defaults, `canonical_output: null`, finite money |
| `python/tests/test_events.py` | The parity test, and decoding of the skill, subagent, child status and tool end classes |
| `python/tests/test_protocol.py` | Tolerant decoding and `UnknownEvent` |

---

Back to the [architecture index](./README.md).
