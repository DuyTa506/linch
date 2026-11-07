//! A response the output limit cut off before it held any text or whole
//! tool call (R2-core 2, P5-Z open point 1): the run fails with an output
//! limit error, the session gets nothing, and the paid call still counts.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::events::model_call::CallDisposition;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{
    Envelope, Event, PermissionMode, ProviderEvent, RunOptions, RunStatus, RunStore,
    ScriptedProvider, ScriptedTurn, StopReason, ToolScope,
};

/// One `Write` call whose input the output limit cut off mid-string.
fn cut_call() -> ScriptedTurn {
    ScriptedTurn {
        events: vec![
            ProviderEvent::MessageStart(MessageStartEvent {
                model: MODEL.into(),
            }),
            ProviderEvent::ToolUseStart(ToolUseStartEvent {
                id: "call_0".into(),
                name: "Write".into(),
            }),
            ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                id: "call_0".into(),
                json_delta: r#"{"value": "a long fi"#.into(),
            }),
            ProviderEvent::MessageEnd(MessageEndEvent {
                stop_reason: StopReason::MaxTokens,
                usage: Some(usage(10, 64)),
                provider_metadata: None,
            }),
        ],
        error: None,
    }
}

/// Only reasoning before the output limit.
fn thinking_only() -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .thinking("first I will", Some("sig"))
        .usage(usage(10, 64))
        .stop_reason(StopReason::MaxTokens)
        .build()
}

/// Run `turn` to its end on fresh stores.
async fn run(turn: ScriptedTurn) -> (Arc<Stores>, Vec<Envelope>, RunStatus) {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let provider = Arc::new(ScriptedProvider::new([turn]));
    let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent(provider, tools, permissions, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    (stores, envelopes, status)
}

#[tokio::test]
async fn an_empty_response_at_the_output_limit_fails_the_run_and_appends_nothing() {
    for (case, turn) in [("cut call", cut_call()), ("thinking", thinking_only())] {
        let (stores, envelopes, status) = run(turn).await;
        assert_eq!(status, RunStatus::Failed, "{case}");
        let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
            panic!("{case}: no result in {:?}", kinds(&envelopes));
        };
        assert_eq!(result.subtype, ResultSubtype::Error, "{case}");
        assert_eq!(result.stop_reason, StopReason::MaxTokens, "{case}");
        assert_eq!(
            result.total_usage,
            usage(10, 64),
            "{case}: the call was paid"
        );
        let error = envelopes.iter().find_map(|e| match &e.event {
            Event::Error(error) => Some(&error.error),
            _ => None,
        });
        let error = error.unwrap_or_else(|| panic!("{case}: no error event"));
        assert_eq!(error["name"], "OutputLimitError", "{case}");
        assert!(
            !kinds(&envelopes).contains(&"assistant".to_owned()),
            "{case}: nothing was answered"
        );
        let session = messages(&*stores, "s1").await;
        assert_eq!(session.len(), 1, "{case}: only the prompt: {session:?}");

        // One terminal write carries the paid receipt, marked rejected.
        let writes = stores.log.lock().unwrap().clone();
        let terminal = ["model_call_receipt", "usage", "error", "result"].map(String::from);
        assert_eq!(
            writes.last(),
            Some(&Write::Commit(terminal.to_vec())),
            "{case}"
        );
        let run_id = &envelopes[0].run_id;
        let events = stores.runs.events(NS, run_id, 0).await.unwrap();
        let receipt = events.iter().find_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt),
            _ => None,
        });
        let receipt = receipt.unwrap_or_else(|| panic!("{case}: no receipt"));
        assert_eq!(receipt.disposition, CallDisposition::Rejected, "{case}");
        assert_eq!(receipt.usage, Some(usage(10, 64)), "{case}");
    }
}

/// Text cut off by the output limit is still an answer.
#[tokio::test]
async fn text_cut_off_by_the_output_limit_still_completes() {
    let turn = TurnBuilder::new(MODEL)
        .text("a partial answer")
        .usage(usage(10, 64))
        .stop_reason(StopReason::MaxTokens)
        .build();
    let (stores, envelopes, status) = run(turn).await;
    assert_eq!(status, RunStatus::Completed);
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.stop_reason, StopReason::MaxTokens);
    assert_eq!(result.final_text.as_deref(), Some("a partial answer"));
    assert_eq!(messages(&*stores, "s1").await.len(), 2);
}
