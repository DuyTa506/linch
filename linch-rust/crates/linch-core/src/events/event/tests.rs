mod samples;

use super::ignorable::KNOWN_EVENT_TYPES;
use super::*;
use samples::one_of_every_kind;
use serde_json::json;

#[test]
fn known_event_types_rejects_already_known_types() {
    // Every tag in the table must be rejected by `IgnorableEvent::new`'s
    // known-type guard; an unlisted tag must not be.
    for tag in KNOWN_EVENT_TYPES {
        assert!(
            IgnorableEvent::new(*tag, JsonObject::new()).is_err(),
            "known type {tag:?} must be rejected by IgnorableEvent::new"
        );
    }
    assert!(IgnorableEvent::new("brand_new_kind", JsonObject::new()).is_ok());
}

/// Drift guard for the crate's two hand-kept event-type lists:
/// `KNOWN_EVENT_TYPES` and the `type_name.as_str()` match arms in
/// `Event::decode`. One sample of every kind (`one_of_every_kind`, minus
/// the open-ended `Ignorable` fallback, which has no wire tag of its own)
/// must encode to a `type` tag set that equals `KNOWN_EVENT_TYPES` exactly,
/// with no duplicate tag on either side; and `decode` must not treat any
/// `KNOWN_EVENT_TYPES` tag as unknown.
#[test]
fn known_event_types_matches_decode_exhaustively() {
    use std::collections::BTreeSet;

    let sample_tags: Vec<String> = one_of_every_kind()
        .iter()
        .filter(|event| !matches!(event, Event::Ignorable(_)))
        .map(
            |event| match event.to_value().expect("sample events are encodable") {
                JsonValue::Object(obj) => match obj.get("type") {
                    Some(JsonValue::String(s)) => s.clone(),
                    _ => panic!("event is missing its type tag"),
                },
                _ => panic!("event did not encode to an object"),
            },
        )
        .collect();

    let sample_set: BTreeSet<&str> = sample_tags.iter().map(String::as_str).collect();
    let known_set: BTreeSet<&str> = KNOWN_EVENT_TYPES.iter().copied().collect();
    assert_eq!(
        sample_set.len(),
        sample_tags.len(),
        "one_of_every_kind must not have two samples with the same type tag"
    );
    assert_eq!(
        sample_set, known_set,
        "KNOWN_EVENT_TYPES must equal the type tags of one_of_every_kind, as a set"
    );

    for tag in KNOWN_EVENT_TYPES {
        let value = json!({"type": tag});
        // A malformed payload (missing required fields) decodes to
        // `InvalidPayload`; only a tag `decode`'s match does not recognize
        // falls to `UnknownType`. Either way, this proves `decode`
        // recognizes `tag`.
        assert!(
            !matches!(
                Event::decode(value, DecodeCtx::strict()),
                Err(EventError::UnknownType(_))
            ),
            "decode must recognize known type {tag:?}"
        );
    }
}

#[test]
fn subagent_persistence_recurses_into_the_wrapped_event() {
    let wrapped_progress = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("child-1"),
        event: Box::new(Event::ToolProgress(ToolProgressEvent {
            tool_use_id: "c1".into(),
            tool_name: "Read".into(),
            message: "working".into(),
            data: None,
        })),
    });
    assert_eq!(wrapped_progress.persistence(), Persistence::StreamOnly);

    use super::super::model_call::{CallDisposition, ModelCallKind, ModelCallReceiptEvent};
    let wrapped_receipt = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("child-1"),
        event: Box::new(Event::ModelCallReceipt(ModelCallReceiptEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            actual_model: None,
            disposition: CallDisposition::Cancelled,
            usage: None,
            cost_usd: None,
            duration_ms: 0,
        })),
    });
    assert_eq!(wrapped_receipt.persistence(), Persistence::JournalOnly);
}

#[test]
fn to_value_surfaces_a_non_finite_monetary_field_as_an_error() {
    let event = Event::Budget(BudgetEvent {
        kind: BudgetKind::Exceeded,
        spent_tokens: 0,
        spent_usd: f64::NAN,
        max_tokens: None,
        max_cost_usd: None,
    });
    assert!(matches!(
        event.to_value(),
        Err(EventError::EncodePayload { ref type_name, .. }) if type_name == "budget"
    ));
}

#[test]
fn to_value_surfaces_a_non_finite_field_nested_inside_a_subagent() {
    let event = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("child-1"),
        event: Box::new(Event::Budget(BudgetEvent {
            kind: BudgetKind::Exceeded,
            spent_tokens: 0,
            spent_usd: f64::INFINITY,
            max_tokens: None,
            max_cost_usd: None,
        })),
    });
    assert!(event.to_value().is_err());
}

#[test]
fn ignorable_new_rejects_empty_type() {
    assert!(matches!(
        IgnorableEvent::new("", JsonObject::new()),
        Err(EventError::EmptyIgnorableType)
    ));
}

#[test]
fn plain_deserialize_is_always_strict() {
    let value = json!({"type": "mystery"});
    assert!(serde_json::from_value::<Event>(value).is_err());
}

#[test]
fn child_run_status_round_trips() {
    let event = Event::ChildRunStatus(ChildRunStatusEvent {
        child_run_id: RunId::new("child-1"),
        status: crate::run::RunStatus::Running,
    });
    let value = event.to_value().unwrap();
    assert_eq!(
        value,
        json!({"type": "child_run_status", "child_run_id": "child-1", "status": "running"})
    );
    assert_eq!(Event::decode(value, DecodeCtx::strict()).unwrap(), event);
}

#[test]
fn subagent_round_trips_and_boxes_its_child() {
    let child = Event::Error(ErrorEvent {
        error: JsonObject::new(),
    });
    let event = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("child-1"),
        event: Box::new(child),
    });
    let value = event.to_value().unwrap();
    assert_eq!(
        value,
        json!({
            "type": "subagent",
            "child_run_id": "child-1",
            "event": {"type": "error", "error": {}},
        })
    );
    assert_eq!(Event::decode(value, DecodeCtx::strict()).unwrap(), event);
}

#[test]
fn persistence_classification_covers_every_kind() {
    use std::collections::HashSet;

    let stream_only: HashSet<&str> = ["tool_progress", "partial_assistant"].into_iter().collect();
    let journal_only: HashSet<&str> = [
        "model_call_delivery",
        "model_call_receipt",
        "model_call_usage_settlement",
    ]
    .into_iter()
    .collect();

    let samples = one_of_every_kind();
    assert_eq!(
        samples.len(),
        KNOWN_EVENT_TYPES.len() + 1,
        "one_of_every_kind must cover every known type plus Ignorable"
    );
    for event in &samples {
        let tag = match event.to_value().expect("sample events are encodable") {
            JsonValue::Object(obj) => match obj.get("type") {
                Some(JsonValue::String(s)) => s.clone(),
                _ => panic!("event is missing its type tag"),
            },
            _ => panic!("event did not encode to an object"),
        };
        let expected = if stream_only.contains(tag.as_str()) {
            Persistence::StreamOnly
        } else if journal_only.contains(tag.as_str()) {
            Persistence::JournalOnly
        } else {
            Persistence::Both
        };
        assert_eq!(event.persistence(), expected, "kind {tag:?}");
    }
}

#[test]
fn every_kind_round_trips_through_decode() {
    use proptest::strategy::{Strategy, ValueTree};
    use proptest::test_runner::{Config, TestRunner};

    let mut runner = TestRunner::new(Config::default());
    for event in one_of_every_kind() {
        let value = event.to_value().expect("sample events are encodable");
        let ctx = if matches!(event, Event::Ignorable(_)) {
            DecodeCtx::root(true)
        } else {
            DecodeCtx::strict()
        };
        assert_eq!(Event::decode(value, ctx).unwrap(), event);
    }
    // Exercise the property over randomly generated values too (bounded
    // subagent nesting), not just the one hand-built sample per kind.
    let strategy = super::super::arbitrary::event(3);
    for _ in 0..256 {
        let tree = strategy.new_tree(&mut runner).unwrap();
        let event = tree.current();
        let value = event.to_value().expect("arbitrary finite events encode");
        let ignorable = contains_ignorable(&event);
        let ctx = DecodeCtx::root(ignorable);
        let decoded = Event::decode(value, ctx).expect("arbitrary event must decode");
        assert_eq!(decoded, event);
    }
}

/// Whether `event`, or any event nested inside it via `subagent`,
/// contains an [`IgnorableEvent`] — i.e. whether decoding it requires
/// an `ignorable` envelope.
fn contains_ignorable(event: &Event) -> bool {
    match event {
        Event::Ignorable(_) => true,
        Event::Subagent(inner) => contains_ignorable(&inner.event),
        _ => false,
    }
}
