use super::*;
use crate::message::{ContentBlock, Message};
use crate::provider::scripted::{ScriptedError, ScriptedProvider, ScriptedTurn};
use crate::provider::{
    MessageEndEvent, MessageStartEvent, StreamError, TextDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use crate::run::StopReason;
use proptest::prelude::*;
use serde_json::json;

mod cancel;

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "m".into(),
        system: Vec::new(),
        tools: Vec::new(),
        appended_tools: Vec::new(),
        messages: vec![Message::user_text("hi")],
        max_output_tokens: None,
        temperature: None,
        stop_sequences: Vec::new(),
        effort: None,
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

fn usage(input: u64, output: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    }
}

fn call() -> ModelCall {
    ModelCall::prepare(&RunId::new("r1"), 2, 1, "m")
}

async fn drive_turn(turn: ScriptedTurn) -> CallOutcome {
    let provider = ScriptedProvider::new([turn]);
    call()
        .drive(&provider, request(), &CancellationToken::new(), None)
        .await
}

fn start() -> ProviderEvent {
    ProviderEvent::MessageStart(MessageStartEvent { model: "m".into() })
}

fn text(text: &str) -> ProviderEvent {
    ProviderEvent::TextDelta(TextDeltaEvent { text: text.into() })
}

fn end(stop_reason: StopReason, usage: Usage) -> ProviderEvent {
    ProviderEvent::MessageEnd(MessageEndEvent {
        stop_reason,
        usage: Some(usage),
        provider_metadata: None,
    })
}

#[test]
fn prepared_ids_depend_only_on_run_turn_and_attempt() {
    let run = RunId::new("r1");
    let first = ModelCall::prepare(&run, 2, 1, "m");
    assert_eq!(first, ModelCall::prepare(&run, 2, 1, "m"));
    let delivery = first.delivery();
    assert_eq!(delivery.logical_call_id, "r1:primary:2");
    assert_eq!(delivery.delivery_attempt_id, "r1:primary:2:1");
    assert_eq!(delivery.kind, ModelCallKind::Primary);
    assert_eq!(delivery.turn_index, Some(2));
    assert_eq!(delivery.requested_model, "m");

    let retry = ModelCall::prepare(&run, 2, 2, "m");
    assert_eq!(retry.delivery().logical_call_id, delivery.logical_call_id);
    assert_ne!(
        retry.delivery().delivery_attempt_id,
        delivery.delivery_attempt_id
    );
    let next_turn = ModelCall::prepare(&run, 3, 1, "m");
    assert_ne!(
        next_turn.delivery().logical_call_id,
        delivery.logical_call_id
    );
}

#[tokio::test]
async fn an_accepted_call_settles_with_the_reported_usage() {
    let turn = ScriptedTurn::builder("served")
        .text("hello")
        .usage(usage(9, 4))
        .build();
    let outcome = drive_turn(turn).await;
    let assembly = outcome.result.unwrap();
    assert_eq!(assembly.message.text(), "hello");
    let receipt = outcome.receipt;
    assert_eq!(receipt.disposition, CallDisposition::Accepted);
    assert_eq!(receipt.usage, Some(usage(9, 4)));
    assert_eq!(receipt.actual_model.as_deref(), Some("served"));
    assert_eq!(receipt.run_id, RunId::new("r1"));
    assert_eq!(receipt.logical_call_id, "r1:primary:2");
    assert_eq!(receipt.delivery_attempt_id, "r1:primary:2:1");
    assert_eq!(receipt.kind, ModelCallKind::Primary);
    assert_eq!(receipt.cost_usd, None);
}

/// A `message_end` without usage settles as accepted with unknown usage,
/// on both the receipt and the assembly.
#[tokio::test]
async fn unreported_usage_stays_unknown() {
    let events = vec![
        start(),
        text("hello"),
        ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: StopReason::EndTurn,
            usage: None,
            provider_metadata: None,
        }),
    ];
    let outcome = drive_turn(ScriptedTurn {
        events,
        error: None,
    })
    .await;
    assert_eq!(outcome.receipt.disposition, CallDisposition::Accepted);
    assert_eq!(outcome.receipt.usage, None);
    assert_eq!(outcome.result.unwrap().usage, None);
}

/// Every failure path still yields one receipt; usage is whatever the
/// provider reported, and unknown stays `None`.
#[tokio::test]
async fn every_failure_path_settles_one_receipt() {
    struct Case {
        name: &'static str,
        turn: ScriptedTurn,
        model: Option<&'static str>,
        usage: Option<Usage>,
        stream_error: bool,
    }
    let malformed_tool = vec![
        start(),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{\"path\":".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        end(StopReason::ToolUse, usage(5, 6)),
    ];
    let cases = [
        Case {
            name: "call fails before any event",
            turn: ScriptedTurn::failing(ScriptedError::Status {
                status: 503,
                retry_after_ms: None,
                message: "busy".into(),
            }),
            model: None,
            usage: None,
            stream_error: false,
        },
        Case {
            name: "stream breaks mid-response",
            turn: ScriptedTurn {
                events: vec![start(), text("par")],
                error: Some(ScriptedError::Transport {
                    message: "reset".into(),
                }),
            },
            model: Some("m"),
            usage: None,
            stream_error: false,
        },
        Case {
            name: "empty stream",
            turn: ScriptedTurn::default(),
            model: None,
            usage: None,
            stream_error: true,
        },
        Case {
            name: "stream ends before message_end",
            turn: ScriptedTurn {
                events: vec![start(), text("par")],
                error: None,
            },
            model: Some("m"),
            usage: None,
            stream_error: true,
        },
        Case {
            name: "end-shape violation keeps its usage",
            turn: ScriptedTurn::builder("m")
                .stop_reason(StopReason::ToolUse)
                .usage(usage(3, 1))
                .build(),
            model: Some("m"),
            usage: Some(usage(3, 1)),
            stream_error: true,
        },
        Case {
            name: "malformed tool input drains to the reported usage",
            turn: ScriptedTurn {
                events: malformed_tool,
                error: None,
            },
            model: Some("m"),
            usage: Some(usage(5, 6)),
            stream_error: true,
        },
    ];
    for case in cases {
        let outcome = drive_turn(case.turn).await;
        let receipt = &outcome.receipt;
        assert_eq!(
            receipt.disposition,
            CallDisposition::Failed,
            "{}",
            case.name
        );
        assert_eq!(receipt.usage, case.usage, "{}", case.name);
        assert_eq!(receipt.actual_model.as_deref(), case.model, "{}", case.name);
        assert_eq!(
            receipt.delivery_attempt_id, "r1:primary:2:1",
            "{}",
            case.name
        );
        let failure = outcome.result.unwrap_err();
        assert_eq!(
            matches!(failure.error, ProviderError::Stream(_)),
            case.stream_error,
            "{}: {:?}",
            case.name,
            failure.error
        );
        assert_eq!(
            failure.event.error["name"], "ProviderError",
            "{}",
            case.name
        );
    }
}

#[tokio::test]
async fn a_failure_carries_its_error_event_payload() {
    let outcome = drive_turn(ScriptedTurn::failing(ScriptedError::Status {
        status: 503,
        retry_after_ms: Some(10),
        message: "busy".into(),
    }))
    .await;
    let failure = outcome.result.unwrap_err();
    assert_eq!(
        JsonValue::Object(failure.event.error),
        json!({
            "name": "ProviderError",
            "kind": "status",
            "message": "provider returned HTTP 503: busy",
            "retryable": true,
            "status": 503,
        })
    );

    let outcome = drive_turn(ScriptedTurn::default()).await;
    let failure = outcome.result.unwrap_err();
    assert!(matches!(
        failure.error,
        ProviderError::Stream(StreamError::EndedBeforeStart)
    ));
    assert_eq!(failure.event.error["kind"], "stream");
    assert_eq!(failure.event.error["retryable"], false);
    assert!(failure.event.error.get("status").is_none());
}

#[test]
fn the_drive_future_is_send() {
    fn assert_send<T: Send>(_: &T) {}
    let provider = ScriptedProvider::new([]);
    let cancel = CancellationToken::new();
    let mut sink = |_: PartialAssistantEvent| {};
    let future = call().drive(&provider, request(), &cancel, Some(&mut sink));
    assert_send(&future);
}

#[derive(Debug, Clone)]
enum Op {
    Text(String),
    Thinking(String, bool),
    Tool(JsonObject),
}

fn op() -> impl Strategy<Value = Op> {
    let input = prop::collection::btree_map("[a-z]{1,3}", any::<i32>(), 0..3).prop_map(|map| {
        map.into_iter()
            .map(|(key, value)| (key, JsonValue::from(value)))
            .collect::<JsonObject>()
    });
    prop_oneof![
        ".{0,6}".prop_map(Op::Text),
        (".{0,6}", any::<bool>()).prop_map(|(text, signed)| Op::Thinking(text, signed)),
        input.prop_map(Op::Tool),
    ]
}

fn delta_field<'a>(delta: &'a PartialAssistantEvent, key: &str) -> &'a str {
    delta.delta[key].as_str().unwrap()
}

proptest! {
    /// The forwarded deltas rebuild exactly the text, thinking and tool
    /// inputs of the assembled message.
    #[test]
    fn forwarded_deltas_equal_the_assembled_message(
        ops in prop::collection::vec(op(), 0..8),
        chunk in 0usize..5,
    ) {
        let mut builder = ScriptedTurn::builder("m").input_chunk(chunk).usage(usage(1, 1));
        for (index, op) in ops.iter().enumerate() {
            builder = match op {
                Op::Text(text) => builder.text(text.clone()),
                Op::Thinking(text, signed) => builder.thinking(text.clone(), signed.then_some("s")),
                Op::Tool(input) => {
                    builder.tool_call(&format!("c{index}"), "T", &JsonValue::Object(input.clone()))
                }
            };
        }
        let provider = ScriptedProvider::new([builder.build()]);
        let mut deltas = Vec::new();
        let mut sink = |delta: PartialAssistantEvent| deltas.push(delta);
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        let outcome = runtime.block_on(call().drive(
            &provider,
            request(),
            &CancellationToken::new(),
            Some(&mut sink),
        ));
        prop_assert_eq!(outcome.receipt.disposition, CallDisposition::Accepted);
        let message = outcome.result.unwrap().message;

        let joined = |kind: &str| -> String {
            deltas
                .iter()
                .filter(|delta| delta_field(delta, "kind") == kind)
                .map(|delta| delta_field(delta, "text"))
                .collect()
        };
        let mut text = String::new();
        let mut thinking = String::new();
        for block in &message.content {
            match block {
                ContentBlock::Text(block) => text.push_str(&block.text),
                ContentBlock::Thinking(block) => thinking.push_str(&block.thinking),
                ContentBlock::ToolUse(block) => {
                    let input: String = deltas
                        .iter()
                        .filter(|delta| delta_field(delta, "kind") == "tool_use_input")
                        .filter(|delta| delta_field(delta, "tool_use_id") == block.id)
                        .map(|delta| delta_field(delta, "json_delta"))
                        .collect();
                    let parsed: JsonObject = serde_json::from_str(&input).unwrap();
                    prop_assert_eq!(&parsed, &block.input);
                }
                other => prop_assert!(false, "unexpected block {:?}", other),
            }
        }
        prop_assert_eq!(text, joined("text"));
        prop_assert_eq!(thinking, joined("thinking"));
    }
}
