use super::*;
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

/// Whether `event`, or any event nested inside it via `subagent`,
/// contains an [`super::super::IgnorableEvent`] — i.e. whether encoding
/// it into an envelope requires `ignorable: true` to decode back.
fn contains_ignorable(event: &Event) -> bool {
    match event {
        Event::Ignorable(_) => true,
        Event::Subagent(inner) => contains_ignorable(&inner.event),
        _ => false,
    }
}

proptest::proptest! {
    /// The actual wire boundary the card describes: an arbitrary event,
    /// wrapped in an [`Envelope`], serialized to JSON text, and parsed
    /// back, round-trips byte-for-byte equal — not just `Event::decode`
    /// on a bare value, which `events::event::tests` already covers.
    #[test]
    fn envelope_round_trips_through_json_text(
        seq in proptest::prelude::any::<u64>(),
        run_id in "[a-zA-Z0-9_-]{1,8}",
        event in super::super::arbitrary::event(3),
    ) {
        let envelope = Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: RunId::new(run_id),
            ignorable: contains_ignorable(&event),
            event,
        };
        let text = serde_json::to_string(&envelope).unwrap();
        let decoded: Envelope = text.parse().unwrap();
        proptest::prop_assert_eq!(decoded, envelope);
    }
}
