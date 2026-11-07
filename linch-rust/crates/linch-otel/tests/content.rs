//! Content capture: off unless switched on, as in Python.

mod common;

use common::*;
use linch_core::{RunOptions, ToolScope};
use opentelemetry_sdk::trace::SpanData;

const CONTENT_KEYS: [&str; 4] = [
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
];

async fn traced(capture: Option<bool>) -> Vec<SpanData> {
    let sdk = Sdk::new();
    let turns = vec![tool_turn(0, 1, "Echo"), text_turn("hello")];
    let agent = agent(turns, vec![Echo::new("Echo", ToolScope::Read)]);
    let observer = sdk.telemetry.observer().provider("scripted");
    let mut observer = match capture {
        Some(on) => observer.capture_content(on),
        None => observer,
    };
    observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);
    sdk.spans()
}

#[tokio::test]
async fn content_is_left_out_by_default() {
    assert!(
        !linch_otel::capture_content_from_env(),
        "run the tests without OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true"
    );
    for spans in [traced(None).await, traced(Some(false)).await] {
        assert_eq!(spans.len(), 6, "{:?}", names(&spans));
        for span in &spans {
            for key in CONTENT_KEYS {
                assert_eq!(attr(span, key), None, "{} has {key}", span.name);
            }
        }
    }
}

#[tokio::test]
async fn with_capture_on_the_prompt_answers_and_tool_io_are_kept() {
    let spans = traced(Some(true)).await;
    let run = span(&spans, "invoke_agent");
    assert_eq!(text(run, "gen_ai.input.messages").unwrap(), r#""hi""#);
    assert_eq!(text(run, "gen_ai.output.messages").unwrap(), r#""hello""#);

    let chats = all(&spans, "chat scripted-model");
    let answer: serde_json::Value =
        serde_json::from_str(&text(chats[1], "gen_ai.output.messages").unwrap()).unwrap();
    assert_eq!(answer["role"], "assistant");
    assert_eq!(answer["content"][0]["text"], "hello");
    // The request's messages are not on the event stream.
    assert!(
        chats
            .iter()
            .all(|c| attr(c, "gen_ai.input.messages").is_none())
    );

    let tool = span(&spans, "execute_tool Echo");
    assert_eq!(
        text(tool, "gen_ai.tool.call.arguments").unwrap(),
        r#"{"value":"v0.0"}"#
    );
    assert_eq!(text(tool, "gen_ai.tool.call.result").unwrap(), "v0.0");
}
