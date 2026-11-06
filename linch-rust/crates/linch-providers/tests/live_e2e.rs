//! A live two-turn tool conversation through the Rust agent on DeepSeek
//! (`deepseek-flash`), with thinking on and off. Each test skips, and
//! passes, when `DEEPSEEK_API_KEY` is unset.
//!
//! Run from `linch-rust/` with the key loaded from the repo's `.env`:
//! `(set -a; . ../.env; set +a; cargo test -p linch-providers --test live_e2e -- --nocapture)`

#[path = "support/live.rs"]
mod support;

use std::sync::atomic::Ordering;

use futures::StreamExt;
use linch_core::events::kinds::ResultSubtype;
use linch_core::{Event, RunOptions};

async fn conversation(test: &str, thinking: bool) {
    let Some(live) = support::live(thinking) else {
        eprintln!("{test}: skipped, DEEPSEEK_API_KEY is not set");
        return;
    };
    let mut stream = live.agent.run(
        "What is the weather in Paris right now?",
        RunOptions::new(test),
    );
    let mut kinds = Vec::new();
    let mut result = None;
    while let Some(item) = stream.next().await {
        let envelope = item.expect("the run continues durably");
        let kind = envelope.event.to_value().unwrap()["type"].clone();
        if kind != "partial_assistant" {
            kinds.push(kind.as_str().unwrap_or_default().to_owned());
        }
        if let Event::Result(done) = envelope.event {
            result = Some(done);
        }
    }
    let calls = live.calls.lock().unwrap().clone();
    for (index, call) in calls.iter().enumerate() {
        eprintln!(
            "{test}: call {} stop={:?} first_event_ms={} total_ms={} usage={:?}",
            index + 1,
            call.stop,
            call.first_event.unwrap_or_default().as_millis(),
            call.total.as_millis(),
            call.usage,
        );
    }
    eprintln!("{test}: events {kinds:?}");
    let result = result.expect("the run ends with a result");
    eprintln!("{test}: final text {:?}", result.final_text);
    assert_eq!(result.subtype, ResultSubtype::Success, "{result:?}");
    assert!(
        result
            .final_text
            .is_some_and(|text| !text.trim().is_empty())
    );
    assert!(result.usage_complete);
    assert!(
        live.weather.runs.load(Ordering::SeqCst) >= 1,
        "the tool ran"
    );
    assert!(calls.len() >= 2, "a tool turn, then the answer");
}

#[tokio::test]
async fn a_tool_conversation_with_thinking_on() {
    conversation("live_e2e_thinking_on", true).await;
}

#[tokio::test]
async fn a_tool_conversation_with_thinking_off() {
    conversation("live_e2e_thinking_off", false).await;
}
