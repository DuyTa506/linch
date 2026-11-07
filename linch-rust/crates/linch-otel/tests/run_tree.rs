//! Scripted runs on the memory stores produce Python's span tree, with
//! times from the envelopes and the GenAI attributes and metrics.

mod common;

use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{AgentSpec, CompactionSpec, PermissionMode, RunOptions, ToolScope, Usage};
use opentelemetry::trace::{SpanKind, Status};
use opentelemetry::{Array, Value};

#[tokio::test]
async fn a_text_run_is_an_agent_span_over_one_step_over_one_chat() {
    let sdk = Sdk::new();
    let agent = agent(vec![text_turn("hello")], vec![]);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    assert_eq!(
        names(&spans),
        ["chat scripted-model", "invoke_agent", "step"]
    );
    let run = span(&spans, "invoke_agent");
    let step = span(&spans, "step");
    let chat = span(&spans, "chat scripted-model");
    assert!(is_root(run));
    assert!(is_child(step, run));
    assert!(is_child(chat, step));
    assert_eq!(chat.span_kind, SpanKind::Client);
    assert_eq!(run.span_kind, SpanKind::Internal);

    // Each span opens and closes on its events' times.
    let run_id = envelopes[0].run_id.as_str();
    assert_eq!(run.start_time, at(ts(&envelopes, "init", 0)));
    assert_eq!(run.end_time, at(ts(&envelopes, "result", 0)));
    assert_eq!(step.start_time, at(ts(&envelopes, "user", 0)));
    assert_eq!(step.end_time, at(ts(&envelopes, "result", 0)));
    assert_eq!(chat.start_time, at(ts(&envelopes, "context_build", 0)));
    assert_eq!(chat.end_time, at(ts(&envelopes, "assistant", 0)));

    assert_eq!(text(run, "gen_ai.operation.name").unwrap(), "invoke_agent");
    assert_eq!(text(run, "gen_ai.provider.name").unwrap(), "scripted");
    assert_eq!(text(run, "gen_ai.request.model").unwrap(), MODEL);
    assert_eq!(text(run, "gen_ai.conversation.id").unwrap(), "s1");
    assert_eq!(text(run, "linch.run_id").unwrap(), run_id);
    assert_eq!(attr(run, "linch.resumed"), Some(&Value::Bool(false)));
    assert_eq!(text(run, "linch.stop_reason").unwrap(), "end_turn");
    assert_eq!(int(run, "gen_ai.usage.input_tokens"), Some(10));
    assert_eq!(int(run, "gen_ai.usage.output_tokens"), Some(5));
    assert_eq!(int(run, "gen_ai.usage.cache_read.input_tokens"), Some(0));
    assert_eq!(
        int(run, "gen_ai.usage.cache_creation.input_tokens"),
        Some(0)
    );
    assert_eq!(attr(run, "linch.turn_index"), None);
    assert_eq!(run.status, Status::Unset);

    assert_eq!(text(step, "gen_ai.operation.name").unwrap(), "step");
    assert_eq!(int(step, "linch.turn_index"), Some(0));
    assert_eq!(text(step, "linch.run_id").unwrap(), run_id);
    assert_eq!(text(step, "gen_ai.request.model").unwrap(), MODEL);

    assert_eq!(text(chat, "gen_ai.operation.name").unwrap(), "chat");
    assert_eq!(text(chat, "gen_ai.provider.name").unwrap(), "scripted");
    assert_eq!(text(chat, "gen_ai.conversation.id").unwrap(), "s1");
    assert_eq!(int(chat, "linch.turn_index"), Some(0));
    let reasons = Value::Array(Array::String(vec!["end_turn".into()]));
    assert_eq!(attr(chat, "gen_ai.response.finish_reasons"), Some(&reasons));
    assert_eq!(int(chat, "gen_ai.usage.input_tokens"), Some(10));
    assert_eq!(int(chat, "gen_ai.usage.output_tokens"), Some(5));
    assert_eq!(chat.status, Status::Unset);
    assert_eq!(error_type(chat), None);
}

#[tokio::test]
async fn each_model_call_records_its_tokens_and_duration() {
    let sdk = Sdk::new();
    let agent = agent(vec![text_turn("hello")], vec![]);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    assert_eq!(sdk.unit("gen_ai.client.token.usage"), "{token}");
    assert_eq!(sdk.unit("gen_ai.client.operation.duration"), "s");
    let mut tokens = sdk.points("gen_ai.client.token.usage");
    tokens.sort_by_key(|p| p.attr("gen_ai.token.type"));
    assert_eq!(tokens.len(), 2);
    for (point, (kind, sum)) in tokens.iter().zip([("input", 10.0), ("output", 5.0)]) {
        assert_eq!(point.attr("gen_ai.token.type").unwrap(), kind);
        assert_eq!(point.attr("gen_ai.operation.name").unwrap(), "chat");
        assert_eq!(point.attr("gen_ai.provider.name").unwrap(), "scripted");
        assert_eq!(point.attr("gen_ai.request.model").unwrap(), MODEL);
        assert_eq!(point.attr("error.type"), None);
        assert_eq!((point.count, point.sum), (1, sum));
    }
    let durations = sdk.points("gen_ai.client.operation.duration");
    assert_eq!(durations.len(), 1);
    let elapsed = ts(&envelopes, "assistant", 0) - ts(&envelopes, "context_build", 0);
    assert_eq!(durations[0].count, 1);
    assert_eq!(durations[0].sum, elapsed as f64 / 1000.0);
    assert_eq!(durations[0].attributes.len(), 3, "{:?}", durations[0]);
}

#[tokio::test]
async fn input_tokens_include_the_cached_ones_as_the_convention_says() {
    let sdk = Sdk::new();
    let usage = Usage {
        input_tokens: 100,
        output_tokens: 5,
        cache_read_tokens: 90_000,
        cache_creation_tokens: 400,
    };
    let turn = TurnBuilder::new(MODEL).text("hello").usage(usage).build();
    let agent = agent(vec![turn], vec![]);
    let mut observer = sdk.observer();
    observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    for span in [
        span(&spans, "invoke_agent"),
        span(&spans, "chat scripted-model"),
    ] {
        assert_eq!(int(span, "gen_ai.usage.input_tokens"), Some(90_500));
        assert_eq!(int(span, "gen_ai.usage.output_tokens"), Some(5));
        let cache_read = int(span, "gen_ai.usage.cache_read.input_tokens");
        assert_eq!(cache_read, Some(90_000));
        let cache_creation = int(span, "gen_ai.usage.cache_creation.input_tokens");
        assert_eq!(cache_creation, Some(400));
    }
    let tokens = sdk.points("gen_ai.client.token.usage");
    let input = (tokens.iter())
        .find(|point| point.attr("gen_ai.token.type").as_deref() == Some("input"))
        .unwrap();
    assert_eq!((input.count, input.sum), (1, 90_500.0));
}

#[tokio::test]
async fn tool_calls_are_spans_under_their_turn_and_each_turn_is_a_step() {
    let sdk = Sdk::new();
    let turns = vec![tool_turn(0, 2, "Echo"), text_turn("done")];
    let agent = agent(turns, vec![Echo::new("Echo", ToolScope::Read)]);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("go", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    let run = span(&spans, "invoke_agent");
    let steps = all(&spans, "step");
    let chats = all(&spans, "chat scripted-model");
    let tools = all(&spans, "execute_tool Echo");
    assert_eq!((steps.len(), chats.len(), tools.len()), (2, 2, 2));
    assert!(steps.iter().all(|step| is_child(step, run)));
    assert_eq!(int(steps[0], "linch.turn_index"), Some(0));
    assert_eq!(int(steps[1], "linch.turn_index"), Some(1));
    assert!(is_child(chats[0], steps[0]) && is_child(chats[1], steps[1]));

    // The first turn ends on its tool results; the next starts there.
    let results = all_ts(&envelopes, "user")[1];
    assert_eq!(steps[0].end_time, at(results));
    assert_eq!(steps[1].start_time, at(results));

    let starts = all_ts(&envelopes, "tool_call_start");
    let ends = all_ts(&envelopes, "tool_call_end");
    let mut ids = Vec::new();
    for (i, tool) in tools.iter().enumerate() {
        assert!(is_child(tool, steps[0]));
        assert_eq!(tool.span_kind, SpanKind::Internal);
        assert_eq!(text(tool, "gen_ai.operation.name").unwrap(), "execute_tool");
        assert_eq!(text(tool, "gen_ai.tool.name").unwrap(), "Echo");
        assert_eq!(text(tool, "gen_ai.conversation.id").unwrap(), "s1");
        assert_eq!(int(tool, "linch.turn_index"), Some(0));
        assert_eq!(attr(tool, "gen_ai.provider.name"), None);
        assert_eq!(tool.start_time, at(starts[i]));
        assert_eq!(tool.end_time, at(ends[i]));
        assert_eq!(error_type(tool), None);
        ids.push(text(tool, "gen_ai.tool.call.id").unwrap());
    }
    ids.sort();
    assert_eq!(ids, ["call_0_0", "call_0_1"]);
    let reasons = Value::Array(Array::String(vec!["tool_use".into()]));
    assert_eq!(
        attr(chats[0], "gen_ai.response.finish_reasons"),
        Some(&reasons)
    );
    assert_eq!(int(run, "gen_ai.usage.input_tokens"), Some(30));
    assert_eq!(sdk.points("gen_ai.client.operation.duration")[0].count, 2);
}

#[tokio::test]
async fn a_compaction_is_a_span_with_its_summary_call_under_the_turn() {
    let sdk = Sdk::new();
    let mut spec = AgentSpec::new(MODEL);
    let mut compaction = CompactionSpec::new(1);
    compaction.keep_recent_turns = 1;
    spec.compaction = Some(compaction);
    let summary = linch_core::provider::scripted::TurnBuilder::new(MODEL)
        .text("the summary")
        .usage(usage(100, 9))
        .build();
    let turns = vec![
        tool_turn(0, 1, "Echo"),
        tool_turn(1, 1, "Echo"),
        summary,
        text_turn("done"),
    ];
    let tools = vec![Echo::new("Echo", ToolScope::Read)];
    let stores = Stores::default();
    let agent = agent_with(spec, turns, tools, PermissionMode::Default, &stores);
    let mut observer = sdk.observer();
    let envelopes = observe(agent.run("go", RunOptions::new("s1")), &mut observer).await;
    drop(observer);
    assert_eq!(
        all_ts(&envelopes, "compaction").len(),
        1,
        "the script compacts once"
    );

    let spans = sdk.spans();
    let steps = all(&spans, "step");
    let compaction = span(&spans, "compaction");
    let chats = all(&spans, "chat scripted-model");
    assert_eq!((steps.len(), chats.len()), (3, 4));
    assert!(is_child(compaction, steps[2]));
    assert_eq!(
        text(compaction, "gen_ai.operation.name").unwrap(),
        "compaction"
    );
    assert_eq!(
        text(compaction, "linch.compaction.strategy").unwrap(),
        "summary"
    );
    assert_eq!(int(compaction, "linch.turn_index"), Some(2));
    let compacted = ts(&envelopes, "compaction", 0);
    let took = envelopes.iter().find_map(|e| match &e.event {
        linch_core::Event::Compaction(c) => Some(c.duration_ms),
        _ => None,
    });
    let started = at(compacted - took.unwrap()).max(steps[2].start_time);
    assert_eq!(compaction.start_time, started);
    assert_eq!(compaction.end_time, at(compacted));

    // The summary call is a chat under the compaction, holding its usage.
    let summary: Vec<_> = chats.iter().filter(|c| is_child(c, compaction)).collect();
    assert_eq!(summary.len(), 1);
    assert_eq!(int(summary[0], "gen_ai.usage.input_tokens"), Some(100));
    let primary = chats.iter().find(|c| is_child(c, steps[2])).unwrap();
    assert_eq!(int(primary, "gen_ai.usage.input_tokens"), Some(10));
    let last_build = *all_ts(&envelopes, "context_build").last().unwrap();
    assert_eq!(primary.start_time, at(last_build));
    let tokens = sdk.points("gen_ai.client.token.usage");
    let input: f64 = tokens
        .iter()
        .filter(|p| p.attr("gen_ai.token.type").as_deref() == Some("input"))
        .map(|p| p.sum)
        .sum();
    assert_eq!(input, 20.0 + 20.0 + 100.0 + 10.0);
}
