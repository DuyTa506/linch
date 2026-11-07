//! Live DeepSeek calls. Each test skips when `DEEPSEEK_API_KEY` is unset.
//!
//! Run from `linch-rust/` with the key loaded from the repo's `.env`:
//! `(set -a; . ../.env; set +a; cargo test -p linch-providers --test live_deepseek -- --nocapture)`

use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::provider::Thinking;
use linch_core::{
    Assembler, AssistantAssembly, ContentBlock, Message, Provider, ProviderRequest,
    ReasoningEffort, Role, StopReason, ToolDefinition, ToolResultBlock,
};
use linch_providers::{DeepSeek, DeepSeekOptions};
use serde_json::json;
use tokio_util::sync::CancellationToken;

const MODEL: &str = "deepseek-v4-flash";

fn provider(test: &str) -> Option<DeepSeek> {
    let Some(options) = DeepSeekOptions::from_env() else {
        eprintln!("{test}: skipped, DEEPSEEK_API_KEY is not set");
        return None;
    };
    Some(DeepSeek::new(options).expect("HTTP client builds"))
}

fn request(thinking: bool, messages: Vec<Message>, tools: Vec<ToolDefinition>) -> ProviderRequest {
    ProviderRequest {
        model: MODEL.into(),
        system: vec![],
        tools,
        appended_tools: vec![],
        messages,
        max_output_tokens: Some(1024),
        temperature: None,
        stop_sequences: vec![],
        effort: thinking.then_some(ReasoningEffort::Low),
        thinking: Some(if thinking {
            Thinking::Enabled {
                budget_tokens: None,
                display: None,
            }
        } else {
            Thinking::Disabled {}
        }),
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

async fn call(test: &str, provider: &DeepSeek, request: ProviderRequest) -> AssistantAssembly {
    let started = Instant::now();
    let mut stream = provider
        .stream(request, CancellationToken::new())
        .await
        .expect("request accepted");
    let mut assembler = Assembler::new();
    let mut first_event: Option<Duration> = None;
    while let Some(event) = stream.next().await {
        first_event.get_or_insert_with(|| started.elapsed());
        assembler
            .push(event.expect("stream event"))
            .expect("valid stream");
    }
    let turn = assembler.finish().expect("complete stream");
    eprintln!(
        "{test}: model={} stop={:?} first_event_ms={} total_ms={} usage={:?}",
        turn.model,
        turn.stop_reason,
        first_event.unwrap_or_default().as_millis(),
        started.elapsed().as_millis(),
        turn.usage,
    );
    turn
}

fn has_thinking(turn: &AssistantAssembly) -> bool {
    turn.message
        .content
        .iter()
        .any(|block| matches!(block, ContentBlock::Thinking(_)))
}

async fn text_turn(test: &str, thinking: bool) {
    let Some(provider) = provider(test) else {
        return;
    };
    let prompt = Message::user_text("Reply with exactly the word: pong");
    let turn = call(test, &provider, request(thinking, vec![prompt], vec![])).await;
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(turn.message.text().to_lowercase().contains("pong"));
    assert_eq!(has_thinking(&turn), thinking);
    assert!(turn.usage.expect("DeepSeek reports usage").output_tokens > 0);
}

async fn tool_round_trip(test: &str, thinking: bool) {
    let Some(provider) = provider(test) else {
        return;
    };
    let tool = ToolDefinition {
        name: "get_weather".into(),
        description: "Current weather for a city.".into(),
        input_schema: json!({
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        })
        .as_object()
        .unwrap()
        .clone(),
    };
    let mut messages = vec![Message::user_text(
        "Use the get_weather tool to get the weather in Paris, then tell me in one sentence.",
    )];
    let first = call(
        test,
        &provider,
        request(thinking, messages.clone(), vec![tool.clone()]),
    )
    .await;
    assert_eq!(first.stop_reason, StopReason::ToolUse);
    assert_eq!(has_thinking(&first), thinking);
    let results: Vec<ContentBlock> = first
        .message
        .tool_uses()
        .map(|call| {
            assert_eq!(call.name, "get_weather");
            ContentBlock::ToolResult(ToolResultBlock::text(
                call.id.clone(),
                "17 degrees Celsius, light rain",
                false,
            ))
        })
        .collect();
    messages.push(first.message);
    messages.push(Message::new(Role::User, results));
    let second = call(test, &provider, request(thinking, messages, vec![tool])).await;
    assert_eq!(second.stop_reason, StopReason::EndTurn);
    assert!(second.message.text().contains("17"), "{:?}", second.message);
}

/// A compacted view opens with two user messages (the prompt, then the
/// summary); `deepseek-reasoner` used to reject successive user messages.
#[tokio::test]
async fn two_user_messages_in_a_row_are_accepted() {
    let test = "two_user_messages_in_a_row_are_accepted";
    let Some(provider) = provider(test) else {
        return;
    };
    let messages = vec![
        Message::user_text("My name is Ada."),
        Message::user_text("Reply with exactly my name."),
    ];
    let turn = call(test, &provider, request(true, messages, vec![])).await;
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(turn.message.text().contains("Ada"), "{:?}", turn.message);
}

#[tokio::test]
async fn text_turn_thinking_on() {
    text_turn("text_turn_thinking_on", true).await;
}

#[tokio::test]
async fn text_turn_thinking_off() {
    text_turn("text_turn_thinking_off", false).await;
}

#[tokio::test]
async fn tool_round_trip_thinking_on() {
    tool_round_trip("tool_round_trip_thinking_on", true).await;
}

#[tokio::test]
async fn tool_round_trip_thinking_off() {
    tool_round_trip("tool_round_trip_thinking_off", false).await;
}
