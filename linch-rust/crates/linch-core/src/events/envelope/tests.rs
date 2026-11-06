use super::*;
use crate::events::{IgnorableEvent, SubagentEvent};
use crate::json::JsonObject;
use serde_json::json;

fn minimal_known_event() -> JsonValue {
    json!({"type": "error", "error": {}})
}

#[test]
fn wrong_version_is_rejected() {
    let value = json!({"v": 2, "seq": 1, "run_id": "r1", "event": minimal_known_event()});
    let err = Envelope::from_value(value).unwrap_err();
    assert!(matches!(
        err,
        EventError::UnsupportedVersion { found: 2, .. }
    ));
}

#[test]
fn unknown_type_is_rejected_when_not_ignorable() {
    let value = json!({
        "v": WIRE_VERSION, "seq": 1, "run_id": "r1",
        "event": {"type": "from_the_future", "x": 1},
    });
    let err = Envelope::from_value(value).unwrap_err();
    assert!(matches!(err, EventError::UnknownType(ref t) if t == "from_the_future"));
}

#[test]
fn unknown_type_round_trips_as_ignorable_when_flagged() {
    let value = json!({
        "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true,
        "event": {"type": "from_the_future", "x": 1},
    });
    let envelope = Envelope::from_value(value.clone()).unwrap();
    assert_eq!(serde_json::to_value(&envelope).unwrap(), value);
}

#[test]
fn malformed_known_event_is_rejected_even_when_ignorable() {
    let value = json!({
        "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true,
        // "error" is known but requires an object "error" field.
        "event": {"type": "error"},
    });
    let err = Envelope::from_value(value).unwrap_err();
    assert!(
        matches!(err, EventError::InvalidPayload { ref type_name, .. } if type_name == "error")
    );
}

#[test]
fn unexpected_envelope_field_is_rejected() {
    let value = json!({
        "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "extra": true,
        "event": minimal_known_event(),
    });
    let err = Envelope::from_value(value).unwrap_err();
    assert!(matches!(err, EventError::UnknownField(ref f) if f == "extra"));
}

#[test]
fn nested_subagent_unknown_type_obeys_the_same_ignorable_rule() {
    let nested = json!({"type": "mystery"});
    let subagent = json!({"type": "subagent", "child_run_id": "child-1", "event": nested});

    let strict = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": subagent.clone()});
    assert!(matches!(
        Envelope::from_value(strict).unwrap_err(),
        EventError::UnknownType(ref t) if t == "mystery"
    ));

    let ignorable = json!({
        "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true, "event": subagent,
    });
    let envelope = Envelope::from_value(ignorable.clone()).unwrap();
    assert_eq!(serde_json::to_value(&envelope).unwrap(), ignorable);
}

#[test]
fn subagent_nesting_past_the_limit_is_rejected() {
    let mut event = json!({"type": "error", "error": {}});
    for depth in 0..=MAX_SUBAGENT_DEPTH {
        event = json!({
            "type": "subagent",
            "child_run_id": format!("child-{depth}"),
            "event": event,
        });
    }
    let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": event});
    let err = Envelope::from_value(value).unwrap_err();
    assert!(matches!(err, EventError::NestingTooDeep { max, .. } if max == MAX_SUBAGENT_DEPTH));
}

#[test]
fn subagent_nesting_at_the_limit_is_accepted() {
    let mut event = json!({"type": "error", "error": {}});
    for depth in 0..MAX_SUBAGENT_DEPTH {
        event = json!({
            "type": "subagent",
            "child_run_id": format!("child-{depth}"),
            "event": event,
        });
    }
    let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": event});
    assert!(Envelope::from_value(value).is_ok());
}

#[test]
fn ignorable_flag_is_omitted_when_false() {
    let value =
        json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": minimal_known_event()});
    let envelope = Envelope::from_value(value.clone()).unwrap();
    assert_eq!(serde_json::to_value(&envelope).unwrap(), value);
}

#[test]
fn encoding_a_non_finite_monetary_field_fails_the_whole_envelope() {
    use crate::events::kinds::{BudgetEvent, BudgetKind};

    let envelope = Envelope {
        v: WIRE_VERSION,
        seq: 1,
        run_id: RunId::new("r1"),
        ignorable: false,
        event: Event::Budget(BudgetEvent {
            kind: BudgetKind::Exceeded,
            spent_tokens: 0,
            spent_usd: f64::NAN,
            max_tokens: None,
            max_cost_usd: None,
        }),
    };
    assert!(serde_json::to_string(&envelope).is_err());
}

#[test]
fn from_str_rejects_invalid_json() {
    use std::str::FromStr;
    assert!(matches!(
        Envelope::from_str("{not json"),
        Err(EventError::InvalidJson(_))
    ));
}

#[test]
fn missing_v_is_rejected() {
    let value = json!({"seq": 1, "run_id": "r1", "event": minimal_known_event()});
    assert!(matches!(
        Envelope::from_value(value).unwrap_err(),
        EventError::MissingField("v")
    ));
}

#[test]
fn missing_seq_is_rejected() {
    let value = json!({"v": WIRE_VERSION, "run_id": "r1", "event": minimal_known_event()});
    assert!(matches!(
        Envelope::from_value(value).unwrap_err(),
        EventError::MissingField("seq")
    ));
}

#[test]
fn missing_run_id_is_rejected() {
    let value = json!({"v": WIRE_VERSION, "seq": 1, "event": minimal_known_event()});
    assert!(matches!(
        Envelope::from_value(value).unwrap_err(),
        EventError::MissingField("run_id")
    ));
}

#[test]
fn missing_event_is_rejected() {
    let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1"});
    assert!(matches!(
        Envelope::from_value(value).unwrap_err(),
        EventError::MissingField("event")
    ));
}

#[test]
fn ignorable_flag_is_forced_true_when_an_ignorable_event_is_constructed_directly() {
    // A caller who builds an `Envelope` by hand (not via `from_value`/
    // decode) and forgets to set `ignorable: true` around an
    // `IgnorableEvent` must still get a self-decodable envelope back.
    let event = Event::Ignorable(IgnorableEvent::new("future_kind", JsonObject::new()).unwrap());
    let envelope = Envelope {
        v: WIRE_VERSION,
        seq: 1,
        run_id: RunId::new("r1"),
        ignorable: false,
        event,
    };
    let decoded: Envelope = serde_json::to_string(&envelope).unwrap().parse().unwrap();
    assert!(decoded.ignorable);
    assert_eq!(decoded.event, envelope.event);
}

#[test]
fn ignorable_flag_is_forced_true_for_a_nested_subagent_ignorable_event() {
    let child = Event::Ignorable(IgnorableEvent::new("future_kind", JsonObject::new()).unwrap());
    let event = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("child-1"),
        event: Box::new(child),
    });
    let envelope = Envelope {
        v: WIRE_VERSION,
        seq: 1,
        run_id: RunId::new("r1"),
        ignorable: false,
        event,
    };
    let decoded: Envelope = serde_json::to_string(&envelope).unwrap().parse().unwrap();
    assert!(decoded.ignorable);
    assert_eq!(decoded.event, envelope.event);
}

proptest::proptest! {
    /// The actual wire boundary the card describes: an arbitrary event,
    /// wrapped in an [`Envelope`], serialized to JSON text, and parsed
    /// back, round-trips — not just `Event::decode` on a bare value, which
    /// `events::event::tests` already covers. `declared` is drawn
    /// independently of `event`, including the case the caller gets
    /// `ignorable` "wrong" (`false` around an event that needs it): the
    /// decoded flag must always be the OR of the two, never just the
    /// caller's `declared` value, and every other field must round-trip
    /// unchanged.
    #[test]
    fn envelope_round_trips_through_json_text(
        seq in proptest::prelude::any::<u64>(),
        run_id in "[a-zA-Z0-9_-]{1,8}",
        declared in proptest::prelude::any::<bool>(),
        event in super::super::arbitrary::event(3),
    ) {
        let envelope = Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: RunId::new(run_id),
            ignorable: declared,
            event,
        };
        let text = serde_json::to_string(&envelope).unwrap();
        let decoded: Envelope = text.parse().unwrap();
        proptest::prop_assert_eq!(decoded.v, envelope.v);
        proptest::prop_assert_eq!(decoded.seq, envelope.seq);
        proptest::prop_assert_eq!(&decoded.run_id, &envelope.run_id);
        proptest::prop_assert_eq!(&decoded.event, &envelope.event);
        proptest::prop_assert_eq!(decoded.ignorable, declared || envelope.event.contains_ignorable());
    }
}
