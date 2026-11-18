//! Approval over RPC: a `Write` in permission mode `default` suspends the
//! run, and `run.approve` allows or denies it.

mod common;

use common::*;
use linch::events::kinds::ResultSubtype;
use linch::{Event, RunId};
use linch_rpc::protocol::method;
use linch_rpc::{ClientError, code};
use serde_json::{Value, json};

struct Suspended {
    rpc: Rpc,
    dir: tempfile::TempDir,
    agent: String,
    run_id: RunId,
}

/// A run that asks to write `new.txt` and has suspended on it.
async fn suspended() -> Suspended {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let input = json!({"file_path": "new.txt", "content": "written\n"});
    let turns = vec![tool_turn("call_1", "Write", input), text_turn("Done.")];
    let agent = rpc.open(scripted(turns, Some(dir.path()), "default")).await;
    let run_id = rpc.start(&agent, "s1", "Write new.txt").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    match ended.last() {
        Event::Suspended(suspended) => assert_eq!(suspended.pending_request_ids, ["call_1"]),
        other => panic!("expected suspended, got {other:?}"),
    }
    assert!(!dir.path().join("new.txt").exists());
    Suspended {
        rpc,
        dir,
        agent,
        run_id,
    }
}

impl Suspended {
    async fn approve(&mut self, decisions: Value) -> Vec<Event> {
        let params = json!({"agent": self.agent, "run_id": self.run_id, "decisions": decisions});
        let accepted = self.rpc.accepted(method::RUN_APPROVE, params).await;
        assert_eq!(accepted, self.run_id);
        let ended = self.rpc.until_end(&self.run_id).await;
        assert!(ended.error.is_none(), "{:?}", ended.error);
        ended.envelopes.into_iter().map(|e| e.event).collect()
    }
}

fn result_subtype(events: &[Event]) -> ResultSubtype {
    match events.last() {
        Some(Event::Result(result)) => result.subtype,
        other => panic!("expected a result, got {other:?}"),
    }
}

/// The text of the `tool_call_end` events.
fn tool_ends(events: &[Event]) -> String {
    let ends: Vec<_> = events
        .iter()
        .filter(|event| matches!(event, Event::ToolCallEnd(_)))
        .map(|event| event.to_value().unwrap())
        .collect();
    serde_json::to_string(&ends).unwrap()
}

#[tokio::test]
async fn approving_over_rpc_runs_the_write() {
    let mut run = suspended().await;
    let events = run.approve(json!({"call_1": {"decision": "allow"}})).await;
    assert_eq!(result_subtype(&events), ResultSubtype::Success);
    let written = std::fs::read_to_string(run.dir.path().join("new.txt")).unwrap();
    assert_eq!(written, "written\n");
}

#[tokio::test]
async fn denying_over_rpc_gives_the_model_the_reason() {
    let mut run = suspended().await;
    let deny = json!({"call_1": {"decision": "deny", "reason": "not today"}});
    let events = run.approve(deny).await;
    assert_eq!(result_subtype(&events), ResultSubtype::Success);
    assert!(
        tool_ends(&events).contains("not today"),
        "{}",
        tool_ends(&events)
    );
    assert!(!run.dir.path().join("new.txt").exists());
}

#[tokio::test]
async fn a_malformed_decision_is_invalid_params() {
    let run = suspended().await;
    for decisions in [
        json!({"call_1": {"decision": "maybe"}}),
        json!({"call_1": {"decision": "allow", "reason": "x"}}),
        json!({"call_1": {"decision": "deny"}}),
    ] {
        let params = json!({"agent": run.agent, "run_id": run.run_id, "decisions": decisions});
        let error = run.rpc.call(method::RUN_APPROVE, params).await.unwrap_err();
        assert!(
            matches!(&error, ClientError::Rpc(e) if e.code == code::INVALID_PARAMS),
            "{error:?}"
        );
    }
}

#[tokio::test]
async fn approving_a_finished_run_ends_with_terminal() {
    let mut run = suspended().await;
    run.approve(json!({"call_1": {"decision": "allow"}})).await;
    let params = json!({"agent": run.agent, "run_id": run.run_id, "decisions": {}});
    run.rpc.accepted(method::RUN_APPROVE, params).await;
    let error = run
        .rpc
        .until_end(&run.run_id)
        .await
        .error
        .expect("terminal");
    assert_eq!(error.code, code::TERMINAL);
}

/// `run.cancel` with the agent handle ends a run suspended for approval
/// that the connection no longer drives: its call gets an aborted result,
/// the run ends `aborted`, and a late approve ends with `Terminal`.
#[tokio::test]
async fn cancel_ends_a_suspended_run_through_the_stores() {
    let mut run = suspended().await;
    let bare = json!({"run_id": run.run_id});
    let cancelled = run.rpc.call(method::RUN_CANCEL, bare).await.unwrap();
    assert_eq!(cancelled, json!({"cancelled": false}), "not driven here");

    let params = json!({"agent": run.agent, "run_id": run.run_id});
    let cancelled = run.rpc.call(method::RUN_CANCEL, params.clone()).await;
    assert_eq!(cancelled.unwrap(), json!({"cancelled": true}));
    let replay = json!({"agent": run.agent, "run_id": run.run_id});
    let events = run.rpc.call(method::RUN_EVENTS, replay).await.unwrap();
    let events: Vec<linch::Envelope> = serde_json::from_value(events["events"].clone()).unwrap();
    let events: Vec<Event> = events.into_iter().map(|e| e.event).collect();
    assert_eq!(result_subtype(&events), ResultSubtype::Aborted);
    let aborted = events.iter().any(|event| {
        matches!(event, Event::ToolCallEnd(end) if end.tool_use_id() == "call_1" && end.result.is_error)
    });
    assert!(aborted, "{events:?}");
    assert!(!run.dir.path().join("new.txt").exists());

    let again = run.rpc.call(method::RUN_CANCEL, params).await.unwrap();
    assert_eq!(again, json!({"cancelled": false}));
    let late = json!({"agent": run.agent, "run_id": run.run_id, "decisions": {}});
    run.rpc.accepted(method::RUN_APPROVE, late).await;
    let error = run
        .rpc
        .until_end(&run.run_id)
        .await
        .error
        .expect("terminal");
    assert_eq!(error.code, code::TERMINAL);
}

/// A store cancel names an open agent and an existing run.
#[tokio::test]
async fn a_store_cancel_needs_an_open_agent_and_a_known_run() {
    let run = suspended().await;
    let params = json!({"agent": "agent-99", "run_id": run.run_id});
    let error = run.rpc.call(method::RUN_CANCEL, params).await.unwrap_err();
    assert_eq!(code_of(error), code::AGENT_NOT_FOUND);
    let params = json!({"agent": run.agent, "run_id": "nope"});
    let error = run.rpc.call(method::RUN_CANCEL, params).await.unwrap_err();
    assert_eq!(code_of(error), code::NOT_FOUND);
}
