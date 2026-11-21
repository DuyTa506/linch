//! A tool call whose final arguments are not a JSON object must not end
//! the run: the call is never run with guessed input, and the run goes on.

mod common;

use std::sync::Arc;

use common::flaky::Flaky;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentSpec, ContentBlock, Event, PermissionMode, ProviderEvent, ProviderRetry, RunOptions,
    ToolResultPart, ToolScope,
};
use serde_json::json;

fn spec_with_fast_retry() -> AgentSpec {
    let mut spec = spec();
    spec.provider_retry = ProviderRetry {
        max_attempts: 2,
        base_delay_ms: 1,
        max_delay_ms: 10,
        jitter: 0.0,
    };
    spec
}

/// The model's tool input stops in the middle of an object and the stream
/// still ends with `tool_use`. The run must neither fail on it nor run the
/// tool: the next call (a retry, or the model after a tool error) answers.
#[tokio::test]
async fn invalid_final_tool_arguments_do_not_fail_the_run() {
    let mut broken = TurnBuilder::new(MODEL)
        .tool_call("c1", "Read", &json!({"value": "a.txt"}))
        .build();
    for event in &mut broken.events {
        if let ProviderEvent::ToolUseInputDelta(delta) = event {
            delta.json_delta = r#"{"value": "a.t"#.into();
        }
    }
    let runs = Runs::default();
    // Two turns: a retry answers from the first, a tool error the model
    // reads (one assistant message behind it) from the second.
    let turns = vec![text_turn("done"), text_turn("done")];
    let provider = Arc::new(Flaky::new(turns).fail(0, MODEL, broken));
    let stores = Arc::new(Stores::default());
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec_with_fast_retry(), provider, tools, permissions, stores);
    let (envelopes, error) = drain(agent.run("go", RunOptions::new("s1"))).await;
    let result = match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result,
        other => panic!("the run has no result: {other:?} (error {error:?})"),
    };
    assert_eq!(
        result.subtype,
        ResultSubtype::Success,
        "a malformed tool call ended the run: {result:?}"
    );
    assert!(
        runs.lock().unwrap().is_empty(),
        "the tool ran with guessed input"
    );
}

/// What the model reads and the log keeps: the call stays in the
/// log with the text it wrote and an empty `input`, and
/// its result is an error that names the tool, the parse error and the
/// start of the text. Nothing is retried: the provider is called twice.
#[tokio::test]
async fn the_model_reads_an_error_for_the_call_and_the_log_keeps_its_text() {
    let mut broken = TurnBuilder::new(MODEL)
        .tool_call("c1", "Read", &json!({"value": "a.txt"}))
        .build();
    for event in &mut broken.events {
        if let ProviderEvent::ToolUseInputDelta(delta) = event {
            delta.json_delta = r#"{"value": "a.t"#.into();
        }
    }
    let runs = Runs::default();
    let provider = Arc::new(Flaky::new(vec![broken, text_turn("done")]));
    let stores = Arc::new(Stores::default());
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec(), provider.clone(), tools, permissions, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(runs.lock().unwrap().is_empty(), "the tool ran");
    assert_eq!(provider.calls().len(), 2, "no retry of the provider call");

    let log = messages(&*stores, "s1").await;
    let ContentBlock::ToolUse(call) = &log[1].content[0] else {
        panic!("the call is not in the log: {:?}", log[1]);
    };
    assert_eq!(call.raw_input.as_deref(), Some(r#"{"value": "a.t"#));
    assert!(call.input.is_empty());
    let ContentBlock::ToolResult(result) = &log[2].content[0] else {
        panic!("no tool result: {:?}", log[2]);
    };
    assert!(result.is_error);
    assert_eq!(result.tool_use_id, "c1");
    let ToolResultPart::Text(text) = &result.content[0] else {
        panic!("not text");
    };
    assert!(
        text.text.starts_with("invalid JSON arguments for Read: "),
        "{}",
        text.text
    );
    assert!(text.text.ends_with(r#"received: {"value": "a.t"#));
    let ended = (envelopes.iter())
        .filter(|e| matches!(e.event, Event::ToolCallEnd(_)))
        .count();
    assert_eq!(ended, 1);
}

/// A model call whose input nests `objects` objects, and the run it ends in:
/// the number of times the tool ran and the model's error for the call.
async fn deep_call(objects: usize) -> (usize, Option<String>) {
    // The root holds `chars`, which makes the test tool return at once.
    let inner = objects - 1;
    let text = format!(
        "{{\"chars\":0,\"a\":{}1{}}}",
        "{\"a\":".repeat(inner),
        "}".repeat(inner)
    );
    let input: serde_json::Value = serde_json::from_str(&text).unwrap();
    let turn = TurnBuilder::new(MODEL)
        .tool_call("c1", "Read", &input)
        .build();
    let runs = Runs::default();
    let provider = Arc::new(Flaky::new(vec![turn, text_turn("done")]));
    let stores = Arc::new(Stores::default());
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec(), provider, tools, permissions, stores.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let ran = runs.lock().unwrap().len();
    let log = messages(&*stores, "s1").await;
    let told = match &log[2].content[0] {
        ContentBlock::ToolResult(result) if result.is_error => match &result.content[0] {
            ToolResultPart::Text(text) => Some(text.text.clone()),
            ToolResultPart::Image(_) => None,
        },
        _ => None,
    };
    (ran, told)
}

/// The stores read back a response only up to the journal's depth limit:
/// an input at the limit runs, one level deeper is refused as a broken
/// call, with the reason, and the run goes on.
#[tokio::test]
async fn a_tool_input_nested_past_the_journal_limit_is_a_broken_call() {
    let limit = linch_core::JOURNAL_MAX_DEPTH + 1;
    let (ran, told) = deep_call(limit).await;
    assert_eq!((ran, told), (1, None), "an input at the limit runs");
    let (ran, told) = deep_call(limit + 1).await;
    assert_eq!(ran, 0, "the tool ran on an input past the limit");
    let told = told.expect("the model is told");
    assert!(
        told.starts_with(
            "invalid JSON arguments for Read: the arguments nest deeper than 64 levels"
        ),
        "{told}"
    );
}
