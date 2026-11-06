//! Tool call ranking and capping on synthetic streams.

use linch_core::events::kinds::{ToolCallEndEvent, ToolCallStartEvent};
use linch_core::{Event, JsonObject, JsonValue, ToolResultBlock};
use serde_json::json;

use crate::reports::{RunReport, build_run_report};
use crate::test_support::envelopes;

fn object(value: JsonValue) -> JsonObject {
    value.as_object().cloned().unwrap_or_default()
}

fn report(events: Vec<Event>) -> RunReport {
    build_run_report(&envelopes(events), None)
}

fn start(id: &str, tool: &str) -> Event {
    Event::ToolCallStart(ToolCallStartEvent {
        tool_use_id: id.into(),
        tool_name: tool.into(),
        input: object(json!({"path": id})),
        summary: format!("{tool} {id}"),
    })
}

fn end(id: &str, tool: &str, text: &str, is_error: bool, duration_ms: u64) -> Event {
    Event::ToolCallEnd(ToolCallEndEvent {
        tool_name: tool.into(),
        result: ToolResultBlock::text(id, text, is_error),
        duration_ms,
        canonical_output: None,
        metadata: JsonObject::new(),
    })
}

#[test]
fn the_slowest_and_failing_tools_are_ranked_and_capped() {
    let durations = [5, 40, 10, 40, 1, 30, 20];
    let mut events = Vec::new();
    for (index, duration) in durations.into_iter().enumerate() {
        let id = format!("c{index}");
        events.push(start(&id, "Read"));
        let text = format!("failure {index}\n{}", "x ".repeat(100));
        events.push(end(&id, "Read", &text, index != 1, duration));
    }
    let report = report(events);
    let tools = &report.summary.tools;
    let order: Vec<&str> = tools
        .top_slowest
        .iter()
        .map(|t| t.tool_use_id.as_str())
        .collect();
    assert_eq!(order, ["c1", "c3", "c5", "c6", "c2"]);
    assert_eq!(
        tools.slowest_tool.as_ref().map(|t| t.tool_use_id.as_str()),
        Some("c1")
    );
    assert_eq!((tools.total_duration_ms, tools.max_duration_ms), (146, 40));
    assert_eq!(tools.average_duration_ms, 20.86);
    let failures: Vec<&str> = tools
        .top_failures
        .iter()
        .map(|f| f.tool_use_id.as_str())
        .collect();
    assert_eq!(failures, ["c0", "c2", "c3", "c4", "c5"]);
    let error = &tools.top_failures[0].error;
    assert!(error.starts_with("failure 0 x x"), "{error}");
    assert_eq!(error.chars().count(), 160);
    assert_eq!(tools.top_failures[0].summary, "Read c0");
}

#[test]
fn an_end_without_a_start_still_counts_as_a_call() {
    let report = report(vec![end("c1", "Grep", "found", false, 3)]);
    let call = &report.tool_calls[0];
    assert_eq!(
        (call.tool_name.as_str(), call.summary.as_str()),
        ("Grep", "")
    );
    assert!(call.input.is_empty());
}
