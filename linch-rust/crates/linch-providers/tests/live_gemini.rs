//! Live Gemini calls. Each test skips when `GEMINI_API_KEY` is unset;
//! `GEMINI_MODEL` overrides the model.
//!
//! Run from `linch-rust/` with the key in the environment:
//! `GEMINI_API_KEY=... cargo test -p linch-providers --test live_gemini -- --nocapture`

use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::provider::ThinkingDisplay;
use linch_core::{
    Assembler, AssistantAssembly, ContentBlock, Message, Provider, ProviderRequest, Role,
    StopReason, Thinking, ToolDefinition, ToolResultBlock,
};
use linch_providers::{Gemini, GeminiOptions};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// The model in the Gemini API reference's examples.
const DEFAULT_MODEL: &str = "gemini-3.8-flash";

fn provider(test: &str) -> Option<Gemini> {
    let Some(options) = GeminiOptions::from_env() else {
        eprintln!("{test}: skipped, GEMINI_API_KEY is not set");
        return None;
    };
    Some(Gemini::new(options).expect("HTTP client builds"))
}

fn request(messages: Vec<Message>, tools: Vec<ToolDefinition>) -> ProviderRequest {
    ProviderRequest {
        model: std::env::var("GEMINI_MODEL").unwrap_or_else(|_| DEFAULT_MODEL.into()),
        system: vec![],
        tools,
        messages,
        max_output_tokens: Some(2048),
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: Some(Thinking::Adaptive {
            display: Some(ThinkingDisplay::Summarized),
        }),
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

async fn call(test: &str, provider: &Gemini, request: ProviderRequest) -> AssistantAssembly {
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

#[tokio::test]
async fn text_turn() {
    let Some(provider) = provider("text_turn") else {
        return;
    };
    let prompt = Message::user_text("Reply with exactly the word: pong");
    let turn = call("text_turn", &provider, request(vec![prompt], vec![])).await;
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(turn.message.text().to_lowercase().contains("pong"));
    assert!(turn.usage.output_tokens > 0);
}

/// The second call replays the first turn's kept parts, so it proves the
/// thought signatures go back where Gemini put them.
#[tokio::test]
async fn tool_round_trip() {
    let test = "tool_round_trip";
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
        request(messages.clone(), vec![tool.clone()]),
    )
    .await;
    assert_eq!(first.stop_reason, StopReason::ToolUse);
    let results: Vec<ContentBlock> = first
        .message
        .tool_uses()
        .map(|call| {
            ContentBlock::ToolResult(ToolResultBlock::text(
                call.id.clone(),
                "17 degrees Celsius, light rain",
                false,
            ))
        })
        .collect();
    messages.push(first.message);
    messages.push(Message::new(Role::User, results));
    let second = call(test, &provider, request(messages, vec![tool])).await;
    assert_eq!(second.stop_reason, StopReason::EndTurn);
    assert!(second.message.text().contains("17"), "{:?}", second.message);
}
