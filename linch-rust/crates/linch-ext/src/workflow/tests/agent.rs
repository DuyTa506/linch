//! Agent runs in a workflow, on the scripted provider: inside a step, and
//! as journaled `agent` calls that continue their run after a crash.

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::Arc;

use futures::StreamExt;
use linch_core::events::kinds::UserEventSubtype::Prompt;
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::scripted::ScriptedError;
use linch_core::{Event, RunOptions, ScriptedTurn, SessionId};
use serde_json::json;

use super::*;
use crate::test_support::{drain, scripted, text_turn, tool_turn};
use crate::workflow::agent::agent_run_id;

const PROMPT: &str = "review the diff";

/// Runs the `review` agent call on `agent` and returns its text.
async fn review(wf: WorkflowContext, agent: linch_core::Agent) -> Result<String, WorkflowError> {
    wf.agent(&agent, "review", PROMPT).await
}

/// The run id of `w`'s first `review` call in its `generation`.
fn run_id(workflow_id: &str, generation: u64) -> linch_core::RunId {
    let call_key = agent_key("review", PROMPT);
    agent_run_id(NS, workflow_id, &call_key, 0, generation)
}

/// `run`'s final text from its stored events.
async fn final_text(agent: &linch_core::Agent, run: &linch_core::RunId) -> Option<String> {
    let events = agent.events(run, 0).await.unwrap();
    events
        .into_iter()
        .find_map(|envelope| match envelope.event {
            Event::Result(result) => result.final_text,
            _ => None,
        })
}

#[tokio::test]
async fn an_agent_run_inside_a_step_replays_without_a_provider_call() {
    let fixture = scripted(vec![text_turn("LGTM")]);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move {
            let review =
                wf.step("review", || {
                    let agent = agent.clone();
                    async move {
                        let run = agent.run("review the diff", RunOptions::new("review-1"));
                        let final_text = drain(run).await.into_iter().find_map(|envelope| {
                            match envelope.event {
                                Event::Result(result) => result.final_text,
                                _ => None,
                            }
                        });
                        Ok::<_, Infallible>(final_text.unwrap_or_default())
                    }
                });
            let verdict = review.await?;
            Ok(verdict.contains("LGTM"))
        }
    };

    let first = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert!(first.unwrap());
    let recorder = Recorder::default();
    let resumed = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert!(resumed.unwrap());
    assert_eq!(fixture.provider.requests().len(), 1);
    let replayed = &recorder.events()[0];
    assert_eq!(replayed.result_text.as_deref(), Some("\"LGTM\""));
}

#[tokio::test]
async fn an_agent_call_is_journaled_and_replays_without_a_provider_call() {
    let fixture = scripted(vec![text_turn("LGTM")]);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf| review(wf, fixture.agent.clone());
    let recorder = Recorder::default();
    let first = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert_eq!(first.unwrap(), "LGTM");
    let kinds = [WorkflowKind::AgentStart, WorkflowKind::AgentEnd];
    assert_eq!(recorder.kinds(), kinds);
    let run = run_id("w1", 0);
    assert_eq!(
        final_text(&fixture.agent, &run).await.as_deref(),
        Some("LGTM")
    );

    let recorder = Recorder::default();
    let resumed = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert_eq!(resumed.unwrap(), "LGTM");
    assert_eq!(fixture.provider.requests().len(), 1);
    let replayed = &recorder.events()[0];
    assert_eq!(replayed.kind, WorkflowKind::AgentReplayed);
    assert_eq!(
        (replayed.title.as_str(), replayed.subagent_type.as_str()),
        ("review", "review")
    );
    assert_eq!(replayed.result_text.as_deref(), Some("LGTM"));
    assert_eq!(stored(&store, "w1").await.records.len(), 1);
}

#[tokio::test]
async fn a_crashed_agent_call_continues_its_run() {
    let turns = vec![tool_turn("c1", "Probe", json!({})), text_turn("done")];
    let fixture = scripted(turns);
    let run = run_id("w2", 0);
    // The crash: the call's run got as far as its tool call, and the
    // workflow never journaled it.
    let options_for_crash = RunOptions {
        run_id: Some(run.clone()),
        ..RunOptions::new(SessionId::new(run.as_str()))
    };
    let mut stream = fixture.agent.run(PROMPT, options_for_crash);
    while let Some(item) = stream.next().await {
        if let Event::ToolCallStart(_) = item.unwrap().event {
            break;
        }
    }
    drop(stream);

    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf| review(wf, fixture.agent.clone());
    let resumed = run_workflow(options(&store, "w2", &Recorder::default()), flow).await;
    assert_eq!(resumed.unwrap(), "done");
    assert_eq!(fixture.provider.requests().len(), 2, "no model call twice");
    let prompts = (fixture.agent.events(&run, 0).await.unwrap().into_iter())
        .filter(|envelope| matches!(&envelope.event, Event::User(user) if user.subtype == Prompt))
        .count();
    assert_eq!(prompts, 1, "the same run continued");
    assert_eq!(
        final_text(&fixture.agent, &run).await.as_deref(),
        Some("done")
    );
}

#[tokio::test]
async fn a_failed_agent_call_runs_a_fresh_run_on_resume() {
    let failing = ScriptedTurn::failing(ScriptedError::InvalidRequest {
        message: "bad request".into(),
    });
    let fixture = scripted(vec![failing, text_turn("LGTM")]);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf| review(wf, fixture.agent.clone());
    let first = run_workflow(options(&store, "w3", &Recorder::default()), flow).await;
    let Err(WorkflowError::Step { name, source }) = first else {
        panic!("{first:?}");
    };
    assert_eq!(name, "review");
    assert!(source.to_string().contains("bad request"), "{source}");
    assert!(stored(&store, "w3").await.records.is_empty());

    let resumed = run_workflow(options(&store, "w3", &Recorder::default()), flow).await;
    assert_eq!(resumed.unwrap(), "LGTM");
    let fresh = run_id("w3", 1);
    assert_eq!(
        final_text(&fixture.agent, &fresh).await.as_deref(),
        Some("LGTM")
    );
}

#[tokio::test]
async fn an_agent_call_waiting_for_approval_finishes_after_the_host_approves() {
    let turns = vec![tool_turn("c1", "Edit", json!({})), text_turn("edited")];
    let fixture = scripted(turns);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf| review(wf, fixture.agent.clone());
    let run = run_id("w4", 0);
    let first = run_workflow(options(&store, "w4", &Recorder::default()), flow).await;
    let Err(WorkflowError::Step { source, .. }) = first else {
        panic!("{first:?}");
    };
    let message = source.to_string();
    assert!(message.contains(run.as_str()) && message.contains("approval"));

    let answers = HashMap::from([("c1".to_owned(), ApprovalResponse::Allow)]);
    let approve = fixture
        .agent
        .approve(run.clone(), answers, RunOptions::new("host"));
    drain(approve).await;
    let resumed = run_workflow(options(&store, "w4", &Recorder::default()), flow).await;
    assert_eq!(resumed.unwrap(), "edited");
    assert_eq!(fixture.provider.requests().len(), 2);
}

#[tokio::test]
async fn a_cancelled_workflow_starts_no_agent_run() {
    let fixture = scripted(vec![text_turn("LGTM")]);
    let store = Arc::new(MemoryJournalStore::default());
    let options = options(&store, "w5", &Recorder::default());
    options.cancel.cancel();
    let flow = |wf| review(wf, fixture.agent.clone());
    let outcome = run_workflow(options, flow).await;
    assert!(
        matches!(outcome, Err(WorkflowError::Aborted { .. })),
        "{outcome:?}"
    );
    assert!(fixture.provider.requests().is_empty());
}
