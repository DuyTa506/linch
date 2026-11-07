//! Live OpenAI calls. Each test skips when `OPENAI_API_KEY` is unset.
//!
//! Run from `linch-rust/` with the key loaded from the repo's `.env`:
//! `(set -a; . ../.env; set +a; cargo test -p linch-providers --test live_openai -- --nocapture)`

use std::time::Instant;

use futures::StreamExt;
use linch_core::{
    Assembler, AssistantAssembly, ContentBlock, Message, Provider, ProviderRequest,
    ReasoningEffort, Role, StopReason, ToolDefinition, ToolResultBlock,
};
use linch_providers::{OpenAiChat, OpenAiChatOptions, OpenAiResponses, ResponsesOptions};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// A reasoning model that still takes tools with reasoning on Chat
/// Completions (from GPT-5.4 on, Chat needs effort `none` for tools).
const MODEL: &str = "gpt-5-mini";

fn chat(test: &str) -> Option<OpenAiChat> {
    let Some(options) = OpenAiChatOptions::from_env() else {
        eprintln!("{test}: skipped, OPENAI_API_KEY is not set");
        return None;
    };
    Some(OpenAiChat::new(options).expect("HTTP client builds"))
}

fn responses(test: &str) -> Option<OpenAiResponses> {
    let Some(options) = ResponsesOptions::from_env() else {
        eprintln!("{test}: skipped, OPENAI_API_KEY is not set");
        return None;
    };
    // Stateless: reasoning comes back encrypted and is replayed from the
    // message metadata, with nothing kept on OpenAI's side.
    let options = ResponsesOptions {
        store: false,
        ..options
    };
    Some(OpenAiResponses::new(options).expect("HTTP client builds"))
}

fn request(messages: Vec<Message>, tools: Vec<ToolDefinition>) -> ProviderRequest {
    ProviderRequest {
        model: MODEL.into(),
        system: vec![],
        tools,
        appended_tools: vec![],
        messages,
        max_output_tokens: Some(2048),
        temperature: None,
        stop_sequences: vec![],
        effort: Some(ReasoningEffort::Low),
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

async fn call(test: &str, provider: &dyn Provider, request: ProviderRequest) -> AssistantAssembly {
    let started = Instant::now();
    let mut stream = provider
        .stream(request, CancellationToken::new())
        .await
        .expect("request accepted");
    let mut assembler = Assembler::new();
    while let Some(event) = stream.next().await {
        assembler
            .push(event.expect("stream event"))
            .expect("valid stream");
    }
    let turn = assembler.finish().expect("complete stream");
    eprintln!(
        "{test}: model={} stop={:?} total_ms={} usage={:?}",
        turn.model,
        turn.stop_reason,
        started.elapsed().as_millis(),
        turn.usage,
    );
    turn
}

fn weather_tool() -> ToolDefinition {
    ToolDefinition {
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
    }
}

async fn text_turn(test: &str, provider: &dyn Provider) {
    let ask = Message::user_text("Reply with exactly: pong");
    let turn = call(test, provider, request(vec![ask], vec![])).await;
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(turn.message.text().to_lowercase().contains("pong"));
    assert!(turn.usage.expect("OpenAI reports usage").output_tokens > 0);
}

async fn tool_round_trip(test: &str, provider: &dyn Provider) {
    let ask = Message::user_text("What is the weather in Hue? Use the tool.");
    let first = call(
        test,
        provider,
        request(vec![ask.clone()], vec![weather_tool()]),
    )
    .await;
    assert_eq!(first.stop_reason, StopReason::ToolUse);
    let call_id = first
        .message
        .tool_uses()
        .next()
        .expect("a tool call")
        .id
        .clone();
    let result = Message::new(
        Role::User,
        vec![ContentBlock::ToolResult(ToolResultBlock::text(
            call_id,
            "31C, sunny",
            false,
        ))],
    );
    let messages = vec![ask, first.message, result];
    let second = call(test, provider, request(messages, vec![weather_tool()])).await;
    assert_eq!(second.stop_reason, StopReason::EndTurn);
    assert!(second.message.text().contains("31"));
}

#[tokio::test]
async fn chat_text_turn() {
    if let Some(provider) = chat("chat_text_turn") {
        text_turn("chat_text_turn", &provider).await;
    }
}

#[tokio::test]
async fn chat_tool_round_trip() {
    if let Some(provider) = chat("chat_tool_round_trip") {
        tool_round_trip("chat_tool_round_trip", &provider).await;
    }
}

#[tokio::test]
async fn responses_text_turn() {
    if let Some(provider) = responses("responses_text_turn") {
        text_turn("responses_text_turn", &provider).await;
    }
}

#[tokio::test]
async fn responses_tool_round_trip_replays_reasoning() {
    if let Some(provider) = responses("responses_tool_round_trip") {
        tool_round_trip("responses_tool_round_trip", &provider).await;
    }
}
