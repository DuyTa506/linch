//! Calls the output limit cut off (R2-edge 3): like Chat and Anthropic, a
//! `max_output_tokens` stop passes through as MaxTokens, so the assembler
//! drops the cut call and keeps the calls that finished.

use linch_core::{StopReason, StreamError, assemble};
use serde_json::{Value, json};

use super::{frames, run_ok, terminal};

fn added(n: u32) -> Value {
    json!({"type": "response.output_item.added", "item": {
        "id": format!("fc_{n}"), "type": "function_call", "status": "in_progress",
        "call_id": format!("c{n}"), "name": "get_weather", "arguments": "",
    }})
}

fn delta(n: u32, arguments: &str) -> Value {
    json!({"type": "response.function_call_arguments.delta",
        "item_id": format!("fc_{n}"), "delta": arguments})
}

fn done(n: u32, status: &str, arguments: &str) -> Value {
    json!({"type": "response.output_item.done", "item": {
        "id": format!("fc_{n}"), "type": "function_call", "status": status,
        "call_id": format!("c{n}"), "name": "get_weather", "arguments": arguments,
    }})
}

fn cut() -> Value {
    terminal(
        "response.incomplete",
        json!({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
    )
}

const PARTIAL: &str = "{\"city\": \"Par";

/// A finished `c1`.
fn first_call() -> Vec<Value> {
    vec![
        added(1),
        delta(1, "{\"city\": \"Hue\"}"),
        done(1, "completed", "{\"city\": \"Hue\"}"),
    ]
}

#[tokio::test]
async fn a_lone_cut_off_call_is_dropped_and_keeps_max_tokens() {
    let text = json!({"type": "response.output_text.delta", "delta": "Checking."});
    // The cut call may or may not get its `output_item.done`.
    for tail in [vec![], vec![done(2, "incomplete", PARTIAL)]] {
        let mut events = vec![text.clone(), added(2), delta(2, PARTIAL)];
        events.extend(tail.clone());
        events.push(cut());
        let turn = assemble(run_ok(&frames(&events)).await).unwrap();
        assert_eq!(turn.stop_reason, StopReason::MaxTokens, "{tail:?}");
        assert_eq!(turn.message.tool_uses().count(), 0);
        assert_eq!(turn.message.text(), "Checking.");
    }
}

#[tokio::test]
async fn a_cut_keeps_the_calls_that_finished() {
    for tail in [vec![], vec![done(2, "incomplete", PARTIAL)]] {
        let mut events = first_call();
        events.extend([added(2), delta(2, PARTIAL)]);
        events.extend(tail.clone());
        events.push(cut());
        let turn = assemble(run_ok(&frames(&events)).await).unwrap();
        assert_eq!(turn.stop_reason, StopReason::ToolUse, "{tail:?}");
        let ids: Vec<_> = (turn.message.tool_uses())
            .map(|call| call.id.as_str())
            .collect();
        assert_eq!(ids, ["c1"]);
    }
}

#[tokio::test]
async fn a_cut_off_call_without_an_output_limit_stop_is_still_invalid() {
    let events = [
        added(1),
        delta(1, PARTIAL),
        done(1, "completed", PARTIAL),
        terminal("response.completed", json!({"status": "completed"})),
    ];
    let err = assemble(run_ok(&frames(&events)).await).unwrap_err();
    assert!(
        matches!(&err, StreamError::InvalidToolInput { id, .. } if id == "c1"),
        "{err:?}"
    );
}

/// As in Python, a done call with no arguments at all reads as `{}` even on
/// a cut, so it is kept; only input that is not a whole object is dropped.
#[tokio::test]
async fn a_done_call_without_arguments_is_kept_on_a_cut() {
    let events = [added(1), done(1, "incomplete", ""), cut()];
    let turn = assemble(run_ok(&frames(&events)).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let call = turn.message.tool_uses().next().unwrap();
    assert!(call.input.is_empty());
}
