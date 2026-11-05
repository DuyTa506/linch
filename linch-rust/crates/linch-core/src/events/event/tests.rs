use super::ignorable::KNOWN_EVENT_TYPES;
use super::*;
use serde_json::json;

#[test]
fn known_event_types_matches_decode_exhaustively() {
    // Every tag `decode` handles explicitly must be in the table (used
    // by `IgnorableEvent::new`'s known-type rejection), and vice versa.
    // `error` is a convenient representative known event to prove the
    // table isn't stale against the match arms below.
    for tag in KNOWN_EVENT_TYPES {
        assert!(
            IgnorableEvent::new(*tag, JsonObject::new()).is_err(),
            "known type {tag:?} must be rejected by IgnorableEvent::new"
        );
    }
    assert!(IgnorableEvent::new("brand_new_kind", JsonObject::new()).is_ok());
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
    let value = event.to_value();
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
    let value = event.to_value();
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

/// One minimal, valid instance of every `Event` kind, built by hand
/// (not via `arbitrary`) so this list is a deliberate, readable
/// enumeration a reviewer can check against the card's kind list.
fn one_of_every_kind() -> Vec<Event> {
    use super::super::model_call::{
        CallDisposition, ModelCallDeliveryEvent, ModelCallKind, ModelCallReceiptEvent,
        ModelCallUsageSettlementEvent,
    };
    use crate::message::ToolResultBlock;
    use crate::run::{RunStatus, StopReason};
    use crate::usage::Usage;

    vec![
        Event::Init(InitEvent {
            session_id: crate::ids::SessionId::new("s1"),
            run_id: RunId::new("r1"),
            parent_run_id: None,
            model: "claude".into(),
            tools: vec![],
            trace_id: None,
        }),
        Event::User(UserEvent {
            message: crate::message::Message::user_text("hi"),
            subtype: UserEventSubtype::Prompt,
        }),
        Event::Assistant(AssistantEvent {
            message: crate::message::Message::assistant_text("hi"),
            stop_reason: StopReason::EndTurn,
        }),
        Event::PartialAssistant(PartialAssistantEvent {
            delta: JsonObject::new(),
        }),
        Event::Usage(UsageEvent {
            usage: Usage::default(),
            cumulative: Usage::default(),
        }),
        Event::ToolCallStart(ToolCallStartEvent {
            tool_use_id: "c1".into(),
            tool_name: "Read".into(),
            input: JsonObject::new(),
            summary: "reading".into(),
        }),
        Event::ToolCallEnd(ToolCallEndEvent {
            tool_name: "Read".into(),
            result: ToolResultBlock::text("c1", "done", false),
            duration_ms: 1,
            canonical_output: None,
        }),
        Event::ToolProgress(ToolProgressEvent {
            tool_use_id: "c1".into(),
            tool_name: "Read".into(),
            message: "working".into(),
            data: None,
        }),
        Event::PermissionRequest(PermissionRequestEvent { requests: vec![] }),
        Event::InputRequest(InputRequestEvent {
            request_id: "req1".into(),
            kind: InputRequestKind::AskUser,
            prompt: "continue?".into(),
        }),
        Event::Suspended(SuspendedEvent {
            status: SuspendStatus::AwaitingApproval,
            pending_request_ids: vec!["c1".into()],
        }),
        Event::ContextBuild(ContextBuildEvent {
            system_blocks: 1,
            messages: 1,
            selected_tools: None,
            budget: JsonObject::new(),
            metadata: JsonObject::new(),
        }),
        Event::Compaction(CompactionEvent {
            messages_before: 10,
            messages_after: 2,
            tokens_before: 100,
            tokens_after: 10,
            strategy: "summarize".into(),
        }),
        Event::LoopGuard(LoopGuardEvent {
            reason: LoopGuardReason::MaxTurns,
            detail: "too many turns".into(),
            action: LoopGuardAction::Stop,
        }),
        Event::Budget(BudgetEvent {
            kind: BudgetKind::Warning,
            spent_tokens: 1,
            spent_usd: 0.1,
            max_tokens: None,
            max_cost_usd: None,
        }),
        Event::PromptCacheAdvisory(PromptCacheAdvisoryEvent {
            reason: PromptCacheAdvisoryReason::ModelChanged,
            detail: "model changed".into(),
        }),
        Event::SkillsLoaded(SkillsLoadedEvent { skills: vec![] }),
        Event::SkillInvoked(SkillInvokedEvent {
            name: "deploy".into(),
            args: None,
            model_override: None,
            allowed_tools: None,
        }),
        Event::SkillCompleted(SkillCompletedEvent {
            name: "deploy".into(),
            is_error: false,
        }),
        Event::Subagent(SubagentEvent {
            child_run_id: RunId::new("child-1"),
            event: Box::new(Event::Error(ErrorEvent {
                error: JsonObject::new(),
            })),
        }),
        Event::ChildRunStatus(ChildRunStatusEvent {
            child_run_id: RunId::new("child-1"),
            status: RunStatus::Running,
        }),
        Event::Workflow(WorkflowEvent {
            kind: WorkflowKind::Phase,
            title: String::new(),
            call_key: String::new(),
            occurrence: 0,
            subagent_type: String::new(),
            result_text: None,
            structured_output: None,
            structured_error: None,
        }),
        Event::Verification(VerificationEvent {
            verifier: "output_schema".into(),
            action: VerificationAction::Retry,
            feedback: String::new(),
            attempt: 1,
        }),
        Event::Error(ErrorEvent {
            error: JsonObject::new(),
        }),
        Event::Result(ResultEvent {
            subtype: ResultSubtype::Success,
            stop_reason: StopReason::EndTurn,
            total_usage: Some(Usage::default()),
            final_text: None,
            structured_output: None,
            structured_error: None,
            trace_id: None,
        }),
        Event::ModelCallDelivery(ModelCallDeliveryEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            turn_index: None,
            requested_model: "claude".into(),
        }),
        Event::ModelCallReceipt(ModelCallReceiptEvent {
            run_id: RunId::new("r1"),
            logical_call_id: "lc1".into(),
            delivery_attempt_id: "da1".into(),
            kind: ModelCallKind::Primary,
            actual_model: None,
            disposition: CallDisposition::Cancelled,
            usage: None,
            cost_usd: None,
            duration_ms: 0,
        }),
        Event::ModelCallUsageSettlement(ModelCallUsageSettlementEvent {
            run_id: RunId::new("r1"),
            budget_owner_id: None,
            logical_call_id: "lc1".into(),
            usage: Usage::default(),
            cost_usd: None,
        }),
        Event::Ignorable(IgnorableEvent::new("future_kind", JsonObject::new()).unwrap()),
    ]
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
        let tag = match event.to_value() {
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
        let value = event.to_value();
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
        let value = event.to_value();
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
