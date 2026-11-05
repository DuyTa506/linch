//! One test per contract rule violation: each builds a stream that breaks
//! exactly one rule and checks it gets exactly that [`StreamError`].

use super::*;

#[test]
fn duplicate_message_start_is_rejected() {
    let err = assemble([start("m"), start("m")]).unwrap_err();
    assert!(matches!(err, StreamError::DuplicateMessageStart));
}

#[test]
fn event_before_message_start_is_rejected() {
    let err = assemble([ProviderEvent::TextDelta(TextDeltaEvent {
        text: "hi".into(),
    })])
    .unwrap_err();
    assert!(matches!(
        err,
        StreamError::MissingMessageStart { kind: "text_delta" }
    ));
}

#[test]
fn blank_model_is_rejected() {
    let err = assemble([start("   ")]).unwrap_err();
    assert!(matches!(err, StreamError::BlankModel));
}

#[test]
fn event_after_message_end_is_rejected() {
    let err = assemble([
        start("m"),
        end(StopReason::EndTurn),
        ProviderEvent::TextDelta(TextDeltaEvent {
            text: "late".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(
        err,
        StreamError::EventAfterEnd { kind: "text_delta" }
    ));
}

#[test]
fn empty_tool_id_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "".into(),
            name: "Read".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::EmptyToolId));
}

#[test]
fn empty_tool_name_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::EmptyToolName));
}

#[test]
fn duplicate_tool_id_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Write".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::DuplicateToolId { id } if id == "c1"));
}

#[test]
fn repeated_tool_names_are_allowed_for_distinct_ids() {
    let assembly = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c2".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c2".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c2".into() }),
        end(StopReason::ToolUse),
    ])
    .expect("distinct ids with the same tool name are valid");
    assert_eq!(assembly.message.content.len(), 2);
}

#[test]
fn input_delta_for_unknown_call_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "ghost".into(),
            json_delta: "{}".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(
        err,
        StreamError::UnknownToolId { id, event: "tool_use_input_delta" } if id == "ghost"
    ));
}

#[test]
fn input_delta_after_the_call_closed_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::UnknownToolId { id, .. } if id == "c1"));
}

#[test]
fn tool_use_end_for_unknown_call_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "ghost".into() }),
    ])
    .unwrap_err();
    assert!(matches!(
        err,
        StreamError::UnknownToolId { id, event: "tool_use_end" } if id == "ghost"
    ));
}

#[test]
fn message_end_with_an_open_call_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        end(StopReason::ToolUse),
    ])
    .unwrap_err();
    assert!(
        matches!(err, StreamError::UnfinishedToolCalls { ids } if ids == vec!["c1".to_string()])
    );
}

#[test]
fn malformed_tool_input_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{not json".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::InvalidToolInput { id, .. } if id == "c1"));
}

#[test]
fn non_object_tool_input_is_rejected() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "[1,2,3]".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::NonObjectToolInput { id } if id == "c1"));
}

#[test]
fn empty_stream_is_rejected() {
    let err = assemble([]).unwrap_err();
    assert!(matches!(err, StreamError::EndedBeforeStart));
}

#[test]
fn stream_missing_message_end_is_rejected() {
    let err = assemble([start("m")]).unwrap_err();
    assert!(matches!(err, StreamError::EndedBeforeEnd));
}

#[test]
fn tool_use_stop_reason_without_a_completed_call_is_rejected() {
    let err = assemble([start("m"), end(StopReason::ToolUse)]).unwrap_err();
    assert!(matches!(err, StreamError::ToolUseWithoutCalls));
}

#[test]
fn completed_calls_require_the_tool_use_stop_reason() {
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        end(StopReason::EndTurn),
    ])
    .unwrap_err();
    assert!(matches!(
        err,
        StreamError::CallsWithoutToolUse {
            stop_reason: StopReason::EndTurn
        }
    ));
}
