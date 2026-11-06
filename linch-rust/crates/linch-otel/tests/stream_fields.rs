//! What the stream carries for the observer beyond the conversation: the
//! provider, the model that served a call, how long a compaction took and
//! a failed tool call's error code.

mod common;

use common::*;
use linch_core::RunOptions;
use linch_core::provider::scripted::TurnBuilder;
use serde_json::json;

#[tokio::test]
async fn the_provider_comes_from_init_when_the_host_names_none() {
    let sdk = Sdk::new();
    let agent = agent(vec![text_turn("hello")], vec![]);
    let mut observer = sdk.telemetry.observer().capture_content(false);
    observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    for name in ["invoke_agent", "step", "chat scripted-model"] {
        let span = span(&spans, name);
        assert_eq!(
            text(span, "gen_ai.provider.name").as_deref(),
            Some("scripted"),
            "{name}"
        );
    }
}

#[tokio::test]
async fn a_chat_names_the_model_that_served_it() {
    let sdk = Sdk::new();
    let served = TurnBuilder::new("served-2026")
        .text("hello")
        .usage(usage(1, 1))
        .build();
    let agent = agent(vec![served], vec![]);
    let mut observer = sdk.observer();
    observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    let chat = span(&spans, "chat scripted-model");
    assert_eq!(
        text(chat, "gen_ai.response.model").as_deref(),
        Some("served-2026")
    );
}

#[test]
fn a_chat_is_named_for_the_model_its_call_requested() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    let mut skill_call = context_build();
    skill_call["model"] = json!("skill-model");
    let events = [
        (100, init()),
        (101, prompt()),
        (102, skill_call),
        (110, assistant("end_turn")),
        (111, usage_event(4, 2)),
        (112, result("success")),
    ];
    for (seq, (ts_ms, event)) in events.into_iter().enumerate() {
        observer.observe(&envelope(seq as u64, ts_ms, event));
    }
    drop(observer);

    let spans = sdk.spans();
    let chat = span(&spans, "chat skill-model");
    assert_eq!(
        text(chat, "gen_ai.request.model").as_deref(),
        Some("skill-model")
    );
    let run = span(&spans, "invoke_agent");
    assert_eq!(text(run, "gen_ai.request.model").as_deref(), Some("m"));
    let points = [
        sdk.points("gen_ai.client.operation.duration"),
        sdk.points("gen_ai.client.token.usage"),
    ];
    for point in points.iter().flatten() {
        assert_eq!(point.attr("gen_ai.request.model").unwrap(), "skill-model");
    }
}

#[test]
fn a_compaction_span_starts_its_duration_before_its_event() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    let compaction = json!({
        "type": "compaction", "messages_before": 5, "messages_after": 2,
        "tokens_before": 50, "tokens_after": 20, "strategy": "summary", "duration_ms": 30,
    });
    let events = [
        (100, init()),
        (101, prompt()),
        (150, usage_event(7, 3)),
        (160, compaction),
        (161, context_build()),
        (170, assistant("end_turn")),
        (171, result("success")),
    ];
    for (seq, (ts_ms, event)) in events.into_iter().enumerate() {
        observer.observe(&envelope(seq as u64, ts_ms, event));
    }
    drop(observer);

    let spans = sdk.spans();
    let compaction = span(&spans, "compaction");
    assert_eq!(
        (compaction.start_time, compaction.end_time),
        (at(130), at(160))
    );
    let chats = all(&spans, "chat m");
    let summary = chats.iter().find(|c| is_child(c, compaction)).unwrap();
    assert_eq!((summary.start_time, summary.end_time), (at(130), at(150)));
    assert_eq!(int(summary, "gen_ai.usage.input_tokens"), Some(7));
}

#[test]
fn a_failed_tool_call_is_marked_with_its_error_code() {
    let sdk = Sdk::new();
    let mut observer = sdk.observer();
    let mut coded = tool_end("c1", true, 1);
    coded["error_code"] = json!("not_found");
    let events = [
        (100, init()),
        (101, prompt()),
        (102, tool_start("c1")),
        (103, coded),
        (104, tool_start("c2")),
        (105, tool_end("c2", true, 1)),
        (106, result("success")),
    ];
    for (seq, (ts_ms, event)) in events.into_iter().enumerate() {
        observer.observe(&envelope(seq as u64, ts_ms, event));
    }
    drop(observer);

    let spans = sdk.spans();
    let tools = all(&spans, "execute_tool Echo");
    let error = |id: &str| {
        let tool = tools
            .iter()
            .find(|t| text(t, "gen_ai.tool.call.id").as_deref() == Some(id));
        error_type(tool.unwrap())
    };
    assert_eq!(error("c1").as_deref(), Some("not_found"));
    assert_eq!(
        error("c2").as_deref(),
        Some("tool_error"),
        "no code: as before"
    );
}
