//! Run reports from real scripted runs, stored runs and synthetic streams.

use linch_core::events::kinds::{
    CompactionEvent, ContextBuildEvent, ErrorEvent, InitEvent, LoopGuardAction, LoopGuardEvent,
    LoopGuardReason, PromptCacheAdvisoryEvent, PromptCacheAdvisoryReason, ResultEvent,
    ResultSubtype, UsageEvent, VerificationAction, VerificationEvent,
};
use linch_core::{
    Event, JsonObject, JsonValue, RunId, RunOptions, RunStatus, RunStore, SessionId, StopReason,
    Usage,
};
use serde_json::json;

use super::*;
use crate::test_support::*;

fn object(value: JsonValue) -> JsonObject {
    value.as_object().cloned().unwrap_or_default()
}

fn report(events: Vec<Event>) -> RunReport {
    build_run_report(&envelopes(events), None)
}

fn build(used: JsonValue, max: JsonValue, trimmed: bool) -> Event {
    Event::ContextBuild(ContextBuildEvent {
        system_blocks: 1,
        messages: 2,
        selected_tools: Some(vec!["Read".into()]),
        budget: object(json!({"used_tokens": used, "max_tokens": max, "trimmed": trimmed})),
        metadata: object(json!({"rag": "p1"})),
    })
}

fn result(subtype: ResultSubtype, usage: Usage) -> Event {
    Event::Result(ResultEvent {
        subtype,
        stop_reason: StopReason::EndTurn,
        total_usage: usage,
        usage_complete: true,
        final_text: Some("all done".into()),
        structured_output: None,
        structured_error: None,
        trace_id: None,
    })
}

/// A run that fails one `Probe` call, retries it, then answers.
async fn probe_run() -> (Fixture, RunId, Vec<Envelope>) {
    let fixture = scripted(vec![
        tool_turn("c1", "Probe", json!({"fail": true})),
        tool_turn("c2", "Probe", json!({})),
        text_turn("done"),
    ]);
    let stream = fixture.agent.run("probe", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let events = drain(stream).await;
    (fixture, run_id, events)
}

#[tokio::test]
async fn a_live_run_folds_into_tool_calls_usage_and_counts() {
    let (_fixture, run_id, events) = probe_run().await;
    let report = build_run_report(&events, None);

    assert_eq!(report.run_id, run_id.to_string());
    assert_eq!(report.session_id, "s1");
    assert_eq!(
        (report.status.as_str(), report.phase.as_deref()),
        ("completed", None)
    );
    assert_eq!(report.event_count, events.len());
    assert_eq!(report.timeline, events);
    let calls: Vec<(&str, &str, bool)> = report
        .tool_calls
        .iter()
        .map(|call| {
            (
                call.tool_use_id.as_str(),
                call.result.as_str(),
                call.is_error,
            )
        })
        .collect();
    assert_eq!(calls, [("c1", "probe failed", true), ("c2", "ok", false)]);
    assert_eq!(report.tool_calls[0].input, object(json!({"fail": true})));
    assert_eq!(report.tool_calls[0].summary, "Probe");
    assert_eq!(report.failed_tool_calls(), 1);

    let summary = &report.summary;
    assert_eq!(summary.event_counts["tool_call_start"], 2);
    assert_eq!(summary.event_counts["result"], 1);
    assert_eq!(
        (summary.usage.input_tokens, summary.usage.output_tokens),
        (30, 15)
    );
    assert_eq!(summary.usage.total_tokens, 45);
    assert_eq!(summary.tools.by_name["Probe"], 2);
    assert_eq!(summary.tools.by_name_errors["Probe"], 1);
    assert_eq!(summary.tools.error_rate, 0.5);
    assert_eq!(summary.tools.top_failures[0].error, "probe failed");
    assert!(report.long_run.quality.completed);
    assert_eq!(report.long_run.quality.failed_tool_calls, 1);
    assert!(!report.long_run.resume.has_checkpoint);
    let final_text = report
        .final_result
        .as_ref()
        .and_then(|r| r.final_text.as_deref());
    assert_eq!(final_text, Some("done"));
}

#[tokio::test]
async fn a_stored_run_reports_its_record_without_journal_only_events() {
    let (fixture, run_id, _) = probe_run().await;
    let report = load_run_report(&*fixture.runs, NS, &run_id).await.unwrap();
    let record = fixture.runs.load(NS, &run_id).await.unwrap();

    assert_eq!(report.status, "completed");
    assert_eq!(
        report.phase.as_deref(),
        Some(phase_name(&record.checkpoint).as_str())
    );
    assert_eq!(report.checkpoint.as_ref(), Some(&record.checkpoint));
    let resume = &report.long_run.resume;
    assert!(resume.has_checkpoint);
    assert_eq!(resume.turn_index, Some(record.checkpoint.turn));
    assert_eq!(
        report.timeline,
        fixture.agent.events(&run_id, 0).await.unwrap()
    );
    let counts = &report.summary.event_counts;
    assert!(
        counts.keys().all(|kind| !kind.starts_with("model_call")),
        "{counts:?}"
    );
    assert!(!counts.contains_key("partial_assistant"));

    let missing = load_run_report(&*fixture.runs, NS, &RunId::new("nope")).await;
    assert!(missing.is_err());
}

#[tokio::test]
async fn a_suspended_run_reports_its_pending_permission() {
    let fixture = scripted(vec![tool_turn("c1", "Edit", json!({}))]);
    let session = SessionId::new("s1");
    let stream = fixture.agent.run("edit", RunOptions::new(session));
    let run_id = stream.run_id().clone();
    let events = drain(stream).await;

    let live = build_run_report(&events, None);
    assert_eq!(live.status, "awaiting_approval");
    assert_eq!(live.permission_requests.len(), 1);
    assert_eq!(live.permission_requests[0].tool_name, "Edit");
    assert_eq!(live.summary.risk.permission_requests, 1);
    assert!(live.final_result.is_none());

    let stored = load_run_report(&*fixture.runs, NS, &run_id).await.unwrap();
    let record = fixture.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::AwaitingApproval);
    assert_eq!(stored.status, "awaiting_approval");
    assert_eq!(stored.phase.as_deref(), Some("awaiting_approval"));
}

#[test]
fn without_a_record_the_status_follows_the_stream() {
    assert_eq!(report(vec![]).status, "unknown");
    let init = Event::Init(InitEvent {
        session_id: SessionId::new("s9"),
        run_id: RunId::new("r9"),
        parent_run_id: None,
        model: "m".into(),
        tools: Vec::new(),
        trace_id: None,
    });
    let running = report(vec![init.clone()]);
    assert_eq!(
        (running.run_id.as_str(), running.status.as_str()),
        ("r9", "running")
    );
    let failed = report(vec![init, result(ResultSubtype::Error, Usage::default())]);
    assert_eq!(failed.status, "error");
    let aborted = report(vec![result(ResultSubtype::Aborted, Usage::default())]);
    assert_eq!(aborted.status, "aborted");
    assert_eq!(aborted.session_id, "");
}

#[test]
fn context_builds_give_utilization_pressure_and_long_run_signals() {
    let report = report(vec![
        build(json!(50), json!(100), false),
        build(json!(91.0), json!(100), true),
        build(json!("n/a"), json!(200), false),
    ]);
    let context = &report.summary.context;
    assert_eq!((context.builds, context.trimmed_builds), (3, 1));
    assert_eq!(
        (context.max_used_tokens, context.max_tokens_seen),
        (Some(91), Some(200))
    );
    assert_eq!(context.max_utilization, Some(0.91));
    assert_eq!(context.pressure, "high");
    let long = &report.long_run.context;
    assert_eq!(
        (long.builds, long.trimmed_builds, long.max_used_tokens),
        (3, 1, Some(91))
    );
    assert_eq!(long.selected_tool_counts["Read"], 3);
    assert_eq!(long.metadata_keys, ["rag"]);
}

#[test]
fn pressure_follows_the_highest_utilization() {
    let pressure = |used: u64, max: u64| {
        report(vec![build(json!(used), json!(max), false)])
            .summary
            .context
            .pressure
    };
    assert_eq!(pressure(74, 100), "none");
    assert_eq!(pressure(75, 100), "moderate");
    assert_eq!(pressure(90, 100), "high");
    assert_eq!(pressure(101, 100), "over");
    assert_eq!(pressure(10, 0), "none");
    assert_eq!(report(vec![]).summary.context.max_utilization, None);
}

#[test]
fn recovery_cache_and_risk_events_are_counted() {
    let compaction = |before, after| {
        Event::Compaction(CompactionEvent {
            messages_before: 10,
            messages_after: 2,
            tokens_before: before,
            tokens_after: after,
            strategy: "summary".into(),
        })
    };
    let verification = |action| {
        Event::Verification(VerificationEvent {
            verifier: "grounding".into(),
            action,
            feedback: String::new(),
            attempt: 1,
        })
    };
    let advisory = |reason| {
        Event::PromptCacheAdvisory(PromptCacheAdvisoryEvent {
            reason,
            detail: String::new(),
        })
    };
    let report = report(vec![
        compaction(900, 300),
        compaction(100, 200),
        verification(VerificationAction::Retry),
        verification(VerificationAction::Stop),
        advisory(PromptCacheAdvisoryReason::ToolSetChanged),
        advisory(PromptCacheAdvisoryReason::ModelChanged),
        advisory(PromptCacheAdvisoryReason::ToolSetChanged),
        Event::LoopGuard(LoopGuardEvent {
            reason: LoopGuardReason::MaxTurns,
            detail: "Maximum turns (1) reached.".into(),
            action: LoopGuardAction::Stop,
        }),
        Event::Error(ErrorEvent {
            error: object(json!({"name": "TurnLimitError"})),
        }),
    ]);
    let recovery = &report.summary.recovery;
    assert_eq!(
        (recovery.compactions, recovery.compaction_tokens_saved),
        (2, 600)
    );
    assert_eq!(recovery.verification_retries, 1);
    let cache = &report.summary.prompt_cache;
    assert_eq!((cache.advisory_count, cache.tool_selection_changes), (3, 2));
    assert_eq!(cache.reasons["model_changed"], 1);
    let risk = &report.summary.risk;
    assert_eq!(
        (risk.loop_guards, risk.errors, risk.verification_retries),
        (1, 1, 1)
    );
    assert_eq!(report.errors[0]["name"], "TurnLimitError");
}

#[test]
fn usage_falls_back_to_the_last_usage_event() {
    let cached = Usage {
        input_tokens: 10,
        output_tokens: 4,
        cache_read_tokens: 30,
        cache_creation_tokens: 0,
    };
    let usage = |cumulative| {
        Event::Usage(UsageEvent {
            usage: Usage::default(),
            cumulative,
        })
    };
    let fallback = report(vec![usage(Usage::default()), usage(cached)]);
    let summary = &fallback.summary.usage;
    assert_eq!((summary.total_tokens, summary.cache_read_ratio), (44, 0.75));
    assert_eq!(fallback.summary.prompt_cache.cache_read_ratio, 0.75);
    // A result's total wins over any usage event.
    let ended = report(vec![
        usage(cached),
        result(ResultSubtype::Success, Usage::default()),
    ]);
    assert_eq!(ended.summary.usage.total_tokens, 0);
}

#[tokio::test]
async fn the_report_renders_as_markdown_and_json() {
    let (_fixture, run_id, events) = probe_run().await;
    let live = build_run_report(&events, None);
    let markdown = live.to_markdown();
    let head = format!("# Linch Run Report: {run_id}\n\n- status: completed\n- phase: <none>\n");
    assert!(markdown.starts_with(&head), "{markdown}");
    for line in [
        "- tool calls: 2 (1 failed)",
        "- total_tokens: 45",
        "- tool error rate: 0.5",
        "- max context utilization: none",
        "- slowest tool: Probe (",
        "## Top Slow Tools",
        "## Failing Tools",
        "## Long-Run Signals\n- context trimmed builds: 0\n- max context tokens: none",
        "## Final\n- subtype: success\n- stop_reason: end_turn\n\ndone",
        "## Tool Calls",
        "| Probe | Probe | true | ",
    ] {
        assert!(markdown.contains(line), "missing {line:?} in\n{markdown}");
    }
    let empty = report(vec![]).to_markdown();
    assert!(empty.starts_with("# Linch Run Report: <unknown>\n"));
    assert!(!empty.contains("## Final") && !empty.contains("## Tool Calls"));

    let value = serde_json::to_value(&live).unwrap();
    assert_eq!(value["final"]["subtype"], "success");
    assert_eq!(value["summary"]["tools"]["total"], 2);
    assert_eq!(value["timeline"][0]["event"]["type"], "init");
    assert_eq!(
        value["timeline"].as_array().map(Vec::len),
        Some(events.len())
    );
}
