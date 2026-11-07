use super::*;
use crate::message::{ContentBlock, TextBlock, ThinkingBlock, ToolUseBlock};
use crate::provider::{StreamError, assemble};
use proptest::prelude::*;
use serde_json::json;

fn request(model: &str, text: &str) -> ProviderRequest {
    ProviderRequest {
        model: model.into(),
        system: Vec::new(),
        tools: Vec::new(),
        appended_tools: Vec::new(),
        messages: vec![crate::message::Message::user_text(text)],
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

async fn collect(stream: ProviderStream) -> Vec<Result<ProviderEvent, ProviderError>> {
    stream.collect().await
}

fn usage(input: u64, output: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    }
}

fn sample_fixture() -> ScriptedFixture {
    ScriptedFixture {
        turns: vec![
            ScriptedTurn::builder("m")
                .thinking("plan", Some("sig"))
                .input_chunk(4)
                .tool_call("c1", "Read", &json!({"path": "a.txt"}))
                .usage(usage(10, 5))
                .build(),
            ScriptedTurn {
                events: vec![ProviderEvent::MessageStart(MessageStartEvent {
                    model: "m".into(),
                })],
                error: Some(ScriptedError::Transport {
                    message: "reset".into(),
                }),
            },
            ScriptedTurn::failing(ScriptedError::Status {
                status: 429,
                retry_after_ms: Some(1500),
                message: "slow down".into(),
            }),
            ScriptedTurn::builder("m").text("done").build(),
        ],
    }
}

#[test]
fn fixture_round_trips_through_json() {
    let fixture = sample_fixture();
    let text = serde_json::to_string_pretty(&fixture).unwrap();
    assert_eq!(
        serde_json::from_str::<ScriptedFixture>(&text).unwrap(),
        fixture
    );
    let wire = serde_json::to_value(&fixture).unwrap();
    assert_eq!(
        wire["turns"][2],
        json!({"error": {"type": "status", "status": 429, "retry_after_ms": 1500, "message": "slow down"}})
    );
    assert_eq!(
        wire["turns"][3]["events"][1],
        json!({"type": "text_delta", "text": "done"})
    );
    assert!(wire["turns"][3].get("error").is_none());
}

#[test]
fn fixture_readers_are_strict() {
    for bad in [
        json!({"turns": [], "extra": 1}),
        json!({"turns": [{"events": [], "pause_ms": 5}]}),
        json!({"turns": [{"error": {"type": "cancelled"}}]}),
        json!({"turns": [{"error": {"type": "transport", "message": "x", "code": 1}}]}),
        json!({"turns": [{"events": [{"type": "text_delta", "text": "x", "extra": 1}]}]}),
    ] {
        assert!(
            serde_json::from_value::<ScriptedFixture>(bad.clone()).is_err(),
            "{bad}"
        );
    }
}

#[test]
fn builder_turns_assemble_to_the_scripted_message() {
    let input = json!({"path": "é/ü.txt", "n": 3});
    let turn = ScriptedTurn::builder("m")
        .thinking("hm", None)
        .thinking("m", Some("sig"))
        .text("a")
        .text("b")
        .input_chunk(3)
        .tool_call("c1", "Read", &input)
        .usage(usage(7, 2))
        .build();
    let deltas: Vec<_> = turn
        .events
        .iter()
        .filter_map(|event| match event {
            ProviderEvent::ToolUseInputDelta(delta) => Some(delta.json_delta.clone()),
            _ => None,
        })
        .collect();
    assert!(deltas.len() > 1);
    assert!(deltas.iter().all(|delta| delta.chars().count() <= 3));
    let assembly = assemble(turn.events).unwrap();
    assert_eq!(assembly.stop_reason, StopReason::ToolUse);
    assert_eq!(assembly.usage, Some(usage(7, 2)));
    assert_eq!(assembly.model, "m");
    assert_eq!(
        assembly.message.content,
        vec![
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "hmm".into(),
                signature: Some("sig".into()),
            }),
            ContentBlock::Text(TextBlock { text: "ab".into() }),
            ContentBlock::ToolUse(ToolUseBlock {
                id: "c1".into(),
                name: "Read".into(),
                input: input.as_object().unwrap().clone(),
            }),
        ]
    );
}

#[test]
fn builder_stop_reason_override_can_script_a_violation() {
    let turn = ScriptedTurn::builder("m")
        .stop_reason(StopReason::ToolUse)
        .build();
    assert!(matches!(
        assemble(turn.events),
        Err(StreamError::ToolUseWithoutCalls { .. })
    ));
}

proptest! {
    #[test]
    fn chunks_rejoin_and_respect_the_size(text in ".{0,40}", size in 0usize..6) {
        let pieces = chunks(&text, size);
        prop_assert_eq!(pieces.concat(), text.clone());
        if size > 0 {
            prop_assert!(pieces.iter().all(|piece| piece.chars().count() <= size));
            prop_assert!(pieces.iter().all(|piece| !piece.is_empty()) || text.is_empty());
        } else {
            prop_assert_eq!(pieces.len(), 1);
        }
    }
}

#[tokio::test]
async fn replays_turns_in_order_and_records_every_request() {
    let provider = ScriptedProvider::from(sample_fixture());
    assert_eq!(provider.id(), "scripted");
    let cancel = CancellationToken::new();

    let first = provider
        .stream(request("m", "one"), cancel.clone())
        .await
        .unwrap();
    let events: Vec<_> = collect(first)
        .await
        .into_iter()
        .map(Result::unwrap)
        .collect();
    assert_eq!(events, sample_fixture().turns[0].events);

    let second = collect(
        provider
            .stream(request("m", "two"), cancel.clone())
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(second.len(), 2);
    assert!(matches!(second[0], Ok(ProviderEvent::MessageStart(_))));
    assert!(matches!(&second[1], Err(ProviderError::Transport { message }) if message == "reset"));

    let third = provider.stream(request("m", "three"), cancel.clone()).await;
    assert!(matches!(
        third,
        Err(ProviderError::Status { status: 429, retry_after: Some(delay), .. })
            if delay == Duration::from_millis(1500)
    ));

    assert_eq!(provider.remaining(), 1);
    assert_eq!(
        provider.requests(),
        vec![
            request("m", "one"),
            request("m", "two"),
            request("m", "three")
        ]
    );
}

#[tokio::test]
async fn running_out_of_turns_is_a_typed_non_retryable_error() {
    let provider = ScriptedProvider::new([ScriptedTurn::builder("m").text("hi").build()]);
    let cancel = CancellationToken::new();
    collect(
        provider
            .stream(request("m", "a"), cancel.clone())
            .await
            .unwrap(),
    )
    .await;
    let Err(error) = provider.stream(request("m", "b"), cancel).await else {
        panic!("expected out of turns");
    };
    assert!(!error.retryable());
    assert!(
        matches!(&error, ProviderError::InvalidRequest { message }
            if *message == OutOfTurns { consumed: 1 }.to_string()),
        "{error:?}"
    );
    assert_eq!(provider.requests().len(), 2);
}

#[tokio::test]
async fn a_cancelled_call_fails_without_taking_a_turn() {
    let provider = ScriptedProvider::new([ScriptedTurn::builder("m").text("hi").build()]);
    let cancel = CancellationToken::new();
    cancel.cancel();
    let result = provider.stream(request("m", "a"), cancel).await;
    assert!(matches!(result, Err(ProviderError::Cancelled)));
    assert_eq!(provider.remaining(), 1);
    assert_eq!(provider.requests().len(), 1);
}

#[tokio::test]
async fn a_stream_ends_with_cancelled_once_its_token_fires() {
    let provider = ScriptedProvider::new([ScriptedTurn::builder("m").text("a").text("b").build()]);
    let cancel = CancellationToken::new();
    let mut stream = provider
        .stream(request("m", "a"), cancel.clone())
        .await
        .unwrap();
    assert!(matches!(
        stream.next().await,
        Some(Ok(ProviderEvent::MessageStart(_)))
    ));
    cancel.cancel();
    assert!(matches!(
        stream.next().await,
        Some(Err(ProviderError::Cancelled))
    ));
    assert!(stream.next().await.is_none());
}

#[tokio::test]
async fn without_recording_keeps_no_requests() {
    let provider =
        ScriptedProvider::new([ScriptedTurn::builder("m").text("hi").build()]).without_recording();
    collect(
        provider
            .stream(request("m", "a"), CancellationToken::new())
            .await
            .unwrap(),
    )
    .await;
    assert!(provider.requests().is_empty());
}

#[test]
fn scripted_errors_map_to_provider_errors() {
    let transport: ProviderError = ScriptedError::Transport {
        message: "x".into(),
    }
    .into();
    assert!(transport.retryable());
    let invalid: ProviderError = ScriptedError::InvalidRequest {
        message: "x".into(),
    }
    .into();
    assert!(matches!(invalid, ProviderError::InvalidRequest { .. }));
    let status: ProviderError = ScriptedError::Status {
        status: 400,
        retry_after_ms: None,
        message: "bad".into(),
    }
    .into();
    assert!(matches!(
        status,
        ProviderError::Status {
            status: 400,
            retry_after: None,
            ..
        }
    ));
}
