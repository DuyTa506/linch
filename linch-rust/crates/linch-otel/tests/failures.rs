//! Failed, aborted, suspended, resumed and interrupted runs, and streams in
//! an order no run produces.

mod common;

use std::collections::HashMap;

use common::*;
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::scripted::ScriptedError;
use linch_core::{AgentSpec, PermissionMode, RunOptions, ScriptedTurn, ToolScope};
use opentelemetry::trace::{Status, TraceContextExt};
use opentelemetry::{Context, Value};
use serde_json::json;

#[tokio::test]
async fn a_failed_provider_call_marks_the_chat_the_step_and_the_run() {
    let sdk = Sdk::new();
    let error = ScriptedError::Status {
        status: 500,
        retry_after_ms: None,
        message: "secret detail".into(),
    };
    let agent = agent(vec![ScriptedTurn::failing(error)], vec![]);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    let chat = span(&spans, "chat scripted-model");
    assert_eq!(error_type(chat).as_deref(), Some("ProviderError"));
    assert_eq!(chat.end_time, at(ts(&envelopes, "error", 0)));
    assert_eq!(chat.status, Status::error(""), "the message stays out");
    assert_eq!(
        error_type(span(&spans, "step")).as_deref(),
        Some("ProviderError")
    );
    let run = span(&spans, "invoke_agent");
    assert_eq!(error_type(run).as_deref(), Some("ProviderError"));
    assert_eq!(text(run, "linch.stop_reason").unwrap(), "error");
    let durations = sdk.points("gen_ai.client.operation.duration");
    assert_eq!(durations.len(), 1);
    assert_eq!(durations[0].attr("error.type").unwrap(), "ProviderError");
    assert!(sdk.points("gen_ai.client.token.usage").is_empty());
}

#[tokio::test]
async fn the_turn_limit_ends_the_run_without_opening_another_step() {
    let sdk = Sdk::new();
    let mut spec = AgentSpec::new(MODEL);
    spec.max_turns = Some(1);
    let turns = vec![tool_turn(0, 1, "Echo")];
    let tools = vec![Echo::new("Echo", ToolScope::Read)];
    let stores = Stores::default();
    let agent = agent_with(spec, turns, tools, PermissionMode::Default, &stores);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("go", RunOptions::new("s1")), &mut observer).await;
    drop(observer);
    assert_eq!(all_ts(&envelopes, "loop_guard").len(), 1);

    let spans = sdk.spans();
    let step = span(&spans, "step");
    assert_eq!(error_type(step), None);
    let run = span(&spans, "invoke_agent");
    assert_eq!(error_type(run).as_deref(), Some("TurnLimitError"));
    assert_eq!(run.end_time, at(ts(&envelopes, "result", 0)));
}

#[test]
fn an_abort_during_a_call_marks_every_open_span_with_the_result_subtype() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    for (seq, (ts_ms, event)) in [
        (100, init()),
        (101, prompt()),
        (105, context_build()),
        (130, result("aborted")),
    ]
    .into_iter()
    .enumerate()
    {
        observer.observe(&envelope(seq as u64 + 1, ts_ms, event));
    }
    drop(observer);

    let spans = sdk.spans();
    let chat = span(&spans, "chat m");
    assert_eq!((chat.start_time, chat.end_time), (at(105), at(130)));
    for name in ["chat m", "step", "invoke_agent"] {
        assert_eq!(error_type(span(&spans, name)).as_deref(), Some("aborted"));
    }
    let durations = sdk.points("gen_ai.client.operation.duration");
    assert_eq!(durations[0].attr("error.type").unwrap(), "aborted");
    assert_eq!(durations[0].sum, 0.025);
}

#[tokio::test]
async fn a_suspended_run_ends_its_spans_and_the_approve_is_a_resumed_run() {
    let sdk = Sdk::new();
    let turns = vec![tool_turn(0, 1, "Write"), text_turn("done")];
    let tools = vec![Echo::new("Write", ToolScope::Write)];
    let stores = Stores::default();
    let spec = AgentSpec::new(MODEL);
    let agent = agent_with(spec, turns, tools, PermissionMode::Default, &stores);
    let mut first = sdk.observer();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let suspended = observe(stream, &mut first).await;
    assert_eq!(suspended.last().unwrap().event.type_name(), "suspended");
    drop(first);
    let spans = sdk.spans();
    let run = span(&spans, "invoke_agent");
    assert_eq!(run.end_time, at(ts(&suspended, "suspended", 0)));
    assert_eq!(error_type(run), None);
    assert_eq!(span(&spans, "step").end_time, run.end_time);

    // The approve continues the trace under a host-held parent span.
    let parent_span = sdk.host_span();
    let parent = Context::new().with_span(parent_span);
    let mut second = sdk
        .observer()
        .parent(parent.clone())
        .session_id("s1")
        .turn(0);
    let allow = HashMap::from([("call_0_0".to_string(), ApprovalResponse::Allow)]);
    let stream = agent.approve(run_id, allow, RunOptions::new("s1"));
    let resumed = observe(stream, &mut second).await;
    assert_eq!(
        second.trace_id(),
        linch_otel::span_trace_id(&parent),
        "the resume joins the parent's trace"
    );
    drop(second);

    let spans = sdk.spans();
    let runs = all(&spans, "invoke_agent");
    assert_eq!(runs.len(), 2);
    let again = runs[1];
    assert_eq!(attr(again, "linch.resumed"), Some(&Value::Bool(true)));
    assert_eq!(again.parent_span_id, parent.span().span_context().span_id());
    assert_eq!(again.start_time, at(resumed[0].ts_ms));
    assert_eq!(text(again, "gen_ai.conversation.id").unwrap(), "s1");
    let steps: Vec<_> = all(&spans, "step")
        .into_iter()
        .filter(|s| is_child(s, again))
        .collect();
    assert_eq!(steps.len(), 2);
    assert_eq!(int(steps[0], "linch.turn_index"), Some(0));
    assert_eq!(int(steps[1], "linch.turn_index"), Some(1));
    assert_eq!(steps[0].start_time, at(ts(&resumed, "tool_call_start", 0)));
    let tool = span(&spans, "execute_tool Write");
    assert!(is_child(tool, steps[0]));
    assert_eq!(error_type(tool), None);
}

#[test]
fn a_resume_without_setters_leaves_unknown_attributes_out() {
    let sdk = Sdk::new();
    let mut observer = sdk.telemetry.observer().capture_content(false);
    observer.observe(&envelope(9, 200, context_build()));
    observer.observe(&envelope(10, 210, result("success")));
    drop(observer);

    let spans = sdk.spans();
    let run = span(&spans, "invoke_agent");
    assert_eq!(attr(run, "linch.resumed"), Some(&Value::Bool(true)));
    assert_eq!(text(run, "linch.run_id").unwrap(), "r1");
    for key in [
        "gen_ai.conversation.id",
        "gen_ai.provider.name",
        "gen_ai.request.model",
    ] {
        assert_eq!(attr(run, key), None, "{key}");
    }
    let step = span(&spans, "step");
    assert_eq!(attr(step, "linch.turn_index"), None);
    assert_eq!(step.start_time, at(200));
    // A chat whose model is unknown is just `chat`.
    assert_eq!(span(&spans, "chat").end_time, at(210));
}

#[test]
fn an_interrupted_tool_is_a_short_span_flagged_interrupted() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    observer.observe(&envelope(7, 500, tool_end("call_0_0", true, 0)));
    let start = json!({
        "type": "tool_call_start", "tool_use_id": "call_0_1", "tool_name": "Echo",
        "input": {}, "summary": "Echo",
    });
    // A denied call: its start and end come together, and it is an error.
    observer.observe(&envelope(8, 501, start));
    observer.observe(&envelope(9, 501, tool_end("call_0_1", true, 0)));
    observer.observe(&envelope(10, 502, result("success")));
    drop(observer);

    let spans = sdk.spans();
    let mut tools = all(&spans, "execute_tool Echo");
    tools.sort_by_key(|t| text(t, "gen_ai.tool.call.id"));
    assert_eq!(tools.len(), 2);
    assert_eq!((tools[0].start_time, tools[0].end_time), (at(500), at(500)));
    assert_eq!(error_type(tools[0]).as_deref(), Some("interrupted"));
    assert_eq!(error_type(tools[1]).as_deref(), Some("tool_error"));
    assert!(tools.iter().all(|t| is_child(t, span(&spans, "step"))));
    let run = span(&spans, "invoke_agent");
    assert_eq!(attr(run, "linch.resumed"), Some(&Value::Bool(true)));
    assert_eq!(error_type(run), None);
}

#[test]
fn events_in_an_order_no_run_produces_never_panic_and_every_span_ends() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    let assistant = json!({
        "type": "assistant", "stop_reason": "end_turn",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    });
    let usage = json!({
        "type": "usage", "usage": {"input_tokens": 1}, "cumulative": {"input_tokens": 1},
    });
    let compaction = json!({
        "type": "compaction", "messages_before": 5, "messages_after": 2,
        "tokens_before": 50, "tokens_after": 20, "strategy": "summary",
    });
    let error = json!({"type": "error", "error": {}});
    let events = [
        tool_end("nobody", false, 9_000),
        assistant.clone(),
        usage.clone(),
        compaction,
        init(),
        context_build(),
        context_build(),
        assistant,
        error,
        usage,
    ];
    for (seq, event) in events.into_iter().enumerate() {
        observer.observe(&envelope(seq as u64, 50 - seq as u64, event));
    }
    // Another run's envelope is not this run's.
    let mut other = envelope(99, 60, result("success"));
    other.run_id = "r2".into();
    observer.observe(&other);
    observer.observe(&envelope(100, 70, result("error")));
    observer.observe(&envelope(101, 80, init()));
    drop(observer);

    let spans = sdk.spans();
    let run = span(&spans, "invoke_agent");
    assert_eq!(run.end_time, at(70));
    assert_eq!(error_type(run).as_deref(), Some("run_error"));
    assert!(spans.iter().all(|s| s.end_time >= s.start_time));
    assert!(
        spans
            .iter()
            .all(|s| s.span_context.trace_id() == run.span_context.trace_id())
    );
}

#[test]
fn a_stream_that_fails_or_is_dropped_ends_its_spans_now() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    observer.observe(&envelope(1, 100, init()));
    observer.observe(&envelope(2, 101, prompt()));
    observer.observe(&envelope(3, 102, context_build()));
    observer.fail("RunError");
    observer.observe(&envelope(4, 103, result("success")));
    drop(observer);
    let spans = sdk.spans();
    assert_eq!(names(&spans), ["chat m", "invoke_agent", "step"]);
    for name in ["chat m", "step", "invoke_agent"] {
        let span = span(&spans, name);
        assert_eq!(error_type(span).as_deref(), Some("RunError"), "{name}");
        assert!(span.end_time > at(103), "{name} ends at the wall clock");
    }

    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    observer.observe(&envelope(1, 100, init()));
    observer.observe(&envelope(2, 101, prompt()));
    observer.observe(&envelope(3, 102, context_build()));
    drop(observer);
    let spans = sdk.spans();
    assert_eq!(spans.len(), 3);
    assert!(spans.iter().all(|s| error_type(s).is_none()));
}
