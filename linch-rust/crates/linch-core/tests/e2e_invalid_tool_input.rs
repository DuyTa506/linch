//! A tool call whose final arguments are not a JSON object must not end
//! the run: the call is never run with guessed input, and the run goes on.

mod common;

use std::sync::Arc;

use common::flaky::Flaky;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentSpec, Event, PermissionMode, ProviderEvent, ProviderRetry, RunOptions, ToolScope,
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
