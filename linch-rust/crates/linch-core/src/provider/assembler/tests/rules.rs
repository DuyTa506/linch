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
    let usage = Usage {
        input_tokens: 11,
        output_tokens: 22,
        cache_read_tokens: 33,
        cache_creation_tokens: 44,
    };
    let err = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        end_with_usage(StopReason::ToolUse, usage),
    ])
    .unwrap_err();
    // The usage and stop reason observed on the message_end that triggered
    // this error must survive it: the caller's spend accounting must never
    // be lost just because the stream also broke a shape rule.
    assert!(matches!(
        err,
        StreamError::UnfinishedToolCalls { ids, usage: got_usage, stop_reason: StopReason::ToolUse }
            if ids == vec!["c1".to_string()] && got_usage == Some(usage)
    ));
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
        end(StopReason::ToolUse),
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
        end(StopReason::EndTurn),
    ])
    .unwrap_err();
    assert!(matches!(err, StreamError::NonObjectToolInput { id } if id == "c1"));
}

/// A call with `input`, closed unless `input` is `None`.
fn call(id: &str, input: Option<&str>) -> Vec<ProviderEvent> {
    let mut events = vec![ProviderEvent::ToolUseStart(ToolUseStartEvent {
        id: id.into(),
        name: "Read".into(),
    })];
    events.push(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
        id: id.into(),
        json_delta: input.unwrap_or(r#"{"path": "a"#).into(),
    }));
    if input.is_some() {
        events.push(ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: id.into() }));
    }
    events
}

/// R1-prov 5: the output limit cut the call off, so the turn reports
/// MaxTokens instead of failing on the broken input.
#[test]
fn a_max_tokens_stop_drops_a_call_the_limit_cut_off() {
    let usage = Usage {
        input_tokens: 9,
        ..Usage::default()
    };
    for cut in [call("c1", Some(r#"{"path": "a"#)), call("c1", None)] {
        let mut events = vec![
            start("m"),
            ProviderEvent::TextDelta(TextDeltaEvent {
                text: "Reading.".into(),
            }),
        ];
        events.extend(cut);
        events.push(end_with_usage(StopReason::MaxTokens, usage));
        let assembly = assemble(events).unwrap();
        assert_eq!(assembly.stop_reason, StopReason::MaxTokens);
        assert_eq!(assembly.message, Message::assistant_text("Reading."));
        assert_eq!(assembly.usage, Some(usage));
    }
}

#[test]
fn a_max_tokens_stop_keeps_the_calls_that_finished() {
    let mut events = vec![start("m")];
    events.extend(call("c1", Some(r#"{"path": "a"}"#)));
    events.extend(call("c2", Some("[1")));
    events.push(end(StopReason::MaxTokens));
    let assembly = assemble(events).unwrap();
    // A turn with calls stops for them, as the adapters already report.
    assert_eq!(assembly.stop_reason, StopReason::ToolUse);
    let ids: Vec<&str> = (assembly.message.tool_uses())
        .map(|call| call.id.as_str())
        .collect();
    assert_eq!(ids, ["c1"]);
}

#[test]
fn a_broken_call_fails_any_other_stop() {
    for stop in [StopReason::ToolUse, StopReason::EndTurn] {
        let mut events = vec![start("m")];
        events.extend(call("c1", Some(r#"{"path": "a"#)));
        events.push(end(stop));
        let err = assemble(events).unwrap_err();
        assert!(matches!(&err, StreamError::InvalidToolInput { id, .. } if id == "c1"));
    }
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
    let usage = Usage {
        input_tokens: 5,
        output_tokens: 6,
        cache_read_tokens: 7,
        cache_creation_tokens: 8,
    };
    let err = assemble([start("m"), end_with_usage(StopReason::ToolUse, usage)]).unwrap_err();
    // Usage must still be observable even though no tool call completed.
    assert!(matches!(
        err,
        StreamError::ToolUseWithoutCalls { usage: got_usage, stop_reason: StopReason::ToolUse }
            if got_usage == Some(usage)
    ));
}

#[test]
fn completed_calls_require_the_tool_use_stop_reason() {
    let usage = Usage {
        input_tokens: 1,
        output_tokens: 2,
        cache_read_tokens: 3,
        cache_creation_tokens: 4,
    };
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
        end_with_usage(StopReason::EndTurn, usage),
    ])
    .unwrap_err();
    // The completed call's usage must not be lost behind the shape error.
    assert!(matches!(
        err,
        StreamError::CallsWithoutToolUse {
            stop_reason: StopReason::EndTurn,
            usage: got_usage,
        } if got_usage == Some(usage)
    ));
}
