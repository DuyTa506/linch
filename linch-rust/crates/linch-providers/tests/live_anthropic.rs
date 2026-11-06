//! Live Messages API calls. Each test skips when `ANTHROPIC_API_KEY` is
//! unset; `ANTHROPIC_BASE_URL` points them at a compatible server.
//!
//! Run from `linch-rust/` with the key in the environment:
//! `ANTHROPIC_API_KEY=... cargo test -p linch-providers --test live_anthropic -- --nocapture`

use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::{
    Assembler, AssistantAssembly, ContentBlock, Message, Provider, ProviderRequest, Role,
    StopReason, Thinking, ToolDefinition, ToolResultBlock,
};
use linch_providers::{Anthropic, AnthropicOptions};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// Accepted by the Claude API and mapped by Anthropic-compatible servers
/// such as DeepSeek's.
const MODEL: &str = "claude-sonnet-4-6";

fn provider(test: &str) -> Option<Anthropic> {
    let Some(options) = AnthropicOptions::from_env() else {
        eprintln!("{test}: skipped, ANTHROPIC_API_KEY is not set");
        return None;
    };
    Some(Anthropic::new(options).expect("HTTP client builds"))
}

fn request(thinking: bool, messages: Vec<Message>, tools: Vec<ToolDefinition>) -> ProviderRequest {
    ProviderRequest {
        model: MODEL.into(),
        system: vec![],
        tools,
        messages,
        max_output_tokens: Some(2048),
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: Some(if thinking {
            Thinking::Enabled {
                budget_tokens: Some(1024),
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

async fn call(test: &str, provider: &Anthropic, request: ProviderRequest) -> AssistantAssembly {
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

/// Whether the turn has a thinking block; every one must be signed so it
/// can be replayed.
fn has_signed_thinking(turn: &AssistantAssembly) -> bool {
    let thinking: Vec<_> = turn
        .message
        .content
        .iter()
        .filter_map(|block| match block {
            ContentBlock::Thinking(block) => Some(block),
            _ => None,
        })
        .collect();
    assert!(thinking.iter().all(|block| block.signature.is_some()));
    !thinking.is_empty()
}

async fn text_turn(test: &str, thinking: bool) {
    let Some(provider) = provider(test) else {
        return;
    };
    let prompt = Message::user_text("Reply with exactly the word: pong");
    let turn = call(test, &provider, request(thinking, vec![prompt], vec![])).await;
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(turn.message.text().to_lowercase().contains("pong"));
    assert_eq!(has_signed_thinking(&turn), thinking);
    assert!(
        turn.usage
            .expect("the endpoint reports usage")
            .output_tokens
            > 0
    );
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
    assert_eq!(has_signed_thinking(&first), thinking);
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
    // The signed thinking goes back unmodified with the tool call.
    messages.push(first.message);
    messages.push(Message::new(Role::User, results));
    let second = call(test, &provider, request(thinking, messages, vec![tool])).await;
    assert_eq!(second.stop_reason, StopReason::EndTurn);
    assert!(second.message.text().contains("17"), "{:?}", second.message);
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
