//! `Agent::fail`: a claim plus one commit ends a run that is not terminal.

mod common;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::{
    Event, PermissionMode, Phase, Provider, ProviderError, ProviderRequest, ProviderStream,
    RunError, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider, StoreError, ToolScope,
};
use tokio_util::sync::CancellationToken;

/// A provider whose call never answers, so a run parks mid-dispatch.
struct Hang;

#[async_trait]
impl Provider for Hang {
    fn id(&self) -> &str {
        "hang"
    }

    async fn stream(
        &self,
        _request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        futures::future::pending().await
    }
}

fn result(envelopes: &[linch_core::Envelope]) -> linch_core::events::kinds::ResultEvent {
    match &envelopes.last().unwrap().event {
        Event::Result(result) => result.clone(),
        other => panic!("expected a result, got {other:?}"),
    }
}

#[tokio::test]
async fn failing_a_suspended_run_ends_it_in_one_write() {
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Echo")));
    let tools = registry(vec![Echo::new("Echo", ToolScope::Write, &runs)]);
    let agent = agent(
        provider,
        tools,
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let before = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(before.status, RunStatus::AwaitingApproval);
    let writes = stores.log.lock().unwrap().len();

    let envelopes = (agent.fail(run_id.clone(), "broken", RunOptions::new("x")))
        .await
        .unwrap();
    assert_eq!(kinds(&envelopes), ["error", "result"]);
    let Event::Error(error) = &envelopes[0].event else {
        unreachable!()
    };
    assert_eq!(error.error["message"], "broken");
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert_eq!(result.total_usage, usage(10, 5));
    assert!(result.usage_complete);
    // One claim and one commit; the seqs continue the run's.
    let log = stores.log.lock().unwrap()[writes..].to_vec();
    assert_eq!(log, [Write::Setup, Write::Commit(kinds(&envelopes))]);
    assert_eq!(envelopes[0].seq, before.last_seq + 1);
    let after = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(
        (after.status, after.epoch),
        (RunStatus::Failed, before.epoch + 1)
    );
    assert_eq!(
        agent.events(&run_id, before.last_seq).await.unwrap(),
        envelopes
    );

    // A failed run is terminal for every entry point, and they write nothing.
    let writes = stores.log.lock().unwrap().len();
    let (_, error) = drain(agent.resume(run_id.clone(), RunOptions::new("x"))).await;
    assert!(
        matches!(error, Some(RunError::Terminal { .. })),
        "{error:?}"
    );
    let approve = agent.approve(run_id.clone(), HashMap::new(), RunOptions::new("x"));
    let (_, error) = drain(approve).await;
    assert!(
        matches!(error, Some(RunError::Terminal { .. })),
        "{error:?}"
    );
    let again = agent
        .fail(run_id.clone(), "again", RunOptions::new("x"))
        .await;
    assert!(matches!(
        again,
        Err(RunError::Terminal {
            status: RunStatus::Failed,
            ..
        })
    ));
    assert_eq!(stores.log.lock().unwrap().len(), writes);
    assert!(runs.lock().unwrap().is_empty(), "the tool never ran");
}

#[tokio::test]
async fn a_run_failed_with_a_call_in_flight_has_unknown_usage() {
    let stores = Arc::new(Stores::default());
    let agent = agent(
        Arc::new(Hang),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    // Drop the stream while the call hangs, like a crash mid-dispatch.
    let parked = tokio::time::timeout(Duration::from_millis(100), drain(stream)).await;
    assert!(parked.is_err());
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert!(matches!(
        record.checkpoint.phase,
        Phase::ProviderPending { .. }
    ));

    let envelopes = agent.fail(run_id, "stuck", RunOptions::new("x")).await;
    let result = result(&envelopes.unwrap());
    assert!(!result.usage_complete);
}

#[tokio::test]
async fn failing_an_unknown_run_is_not_found() {
    let stores = Arc::new(Stores::default());
    let agent = agent(
        Arc::new(Hang),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let missing = agent
        .fail(RunId::new("nope"), "x", RunOptions::new("x"))
        .await;
    assert!(matches!(
        missing,
        Err(RunError::Store(StoreError::NotFound { .. }))
    ));
    assert!(stores.log.lock().unwrap().is_empty());
}
