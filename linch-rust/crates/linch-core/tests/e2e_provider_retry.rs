//! Provider reliability: a retryable failure is retried on the same model
//! after a backoff, within `provider_retry.max_attempts`; cancelling or
//! dropping the run during the backoff stops it; and with the policy off,
//! or no failure, a run is exactly what it was without one.

mod common;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use common::flaky::{Flaky, busy};
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::{ProviderRetryEvent, ResultSubtype};
use linch_core::events::model_call::CallDisposition;
use linch_core::provider::scripted::{ScriptedError, ScriptedTurn, TurnBuilder};
use linch_core::{
    Agent, AgentSpec, Commit, Envelope, Event, Lease, LogEntry, NewRun, PermissionMode,
    ProviderRetry, RunId, RunOptions, RunRecord, RunStatus, RunStore, ScriptedProvider, SessionId,
    SessionStore, StoreError, ToolScope,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// A policy that waits `base_delay_ms` doubled, with no jitter.
fn retrying(max_attempts: u32, base_delay_ms: u64) -> AgentSpec {
    let mut spec = spec();
    spec.provider_retry = ProviderRetry {
        max_attempts,
        base_delay_ms,
        max_delay_ms: 1_000,
        jitter: 0.0,
    };
    spec
}

fn flaky_agent(spec: AgentSpec, provider: Arc<Flaky>, stores: Arc<Stores>) -> Agent {
    let permissions = policy(PermissionMode::SkipDangerous);
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
    agent_with(spec, provider, tools, permissions, stores)
}

fn retries(envelopes: &[Envelope]) -> Vec<ProviderRetryEvent> {
    (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ProviderRetry(retry) => Some(retry.clone()),
            _ => None,
        })
        .collect()
}

fn dispositions(envelopes: &[Envelope]) -> Vec<CallDisposition> {
    (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt.disposition),
            _ => None,
        })
        .collect()
}

async fn durable(stores: &Stores, run_id: &RunId) -> Vec<Envelope> {
    stores.runs.events(NS, run_id, 0).await.unwrap()
}

fn result(envelopes: &[Envelope]) -> &linch_core::events::kinds::ResultEvent {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result,
        other => panic!("the run has no result: {other:?}"),
    }
}

/// A 503 is retried on the same model: the failed call settles as its own
/// delivery, the `provider_retry` event goes out with the failed receipt in
/// one write before the wait, and the next call answers.
#[tokio::test]
async fn a_retryable_failure_is_called_again_and_the_run_completes() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, busy()));
    let stores = Arc::new(Stores::default());
    let stream = flaky_agent(retrying(3, 1), provider.clone(), stores.clone())
        .run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    assert_eq!(result(&envelopes).final_text.as_deref(), Some("done"));
    let [retry] = retries(&envelopes).try_into().unwrap();
    assert_eq!(
        (retry.model.as_str(), retry.retry, retry.delay_ms),
        (MODEL, 1, 1)
    );
    assert_eq!(retry.error["status"], 503);
    assert_eq!(retry.error["retryable"], true);
    assert_eq!(provider.calls(), [(0, MODEL.into()), (0, MODEL.into())]);
    let events = durable(&stores, &run_id).await;
    assert_eq!(
        dispositions(&events),
        [CallDisposition::Failed, CallDisposition::Accepted]
    );
    let writes = stores.log.lock().unwrap().clone();
    let wait = Write::Commit(
        ["model_call_receipt", "provider_retry"]
            .map(String::from)
            .into(),
    );
    assert!(writes.contains(&wait), "{writes:?}");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}

/// A call whose stream broke after deltas is called again too: the deltas
/// were stream-only, and the answer comes whole from the next call.
#[tokio::test]
async fn a_stream_broken_mid_answer_is_called_again() {
    let mut broken = TurnBuilder::new(MODEL).text("parti").build();
    broken.events.pop(); // no message_end: the stream breaks mid-answer
    broken.error = Some(ScriptedError::Transport {
        message: "connection reset".into(),
    });
    let provider = Arc::new(Flaky::new(vec![text_turn("whole")]).fail(0, MODEL, broken));
    let stores = Arc::new(Stores::default());
    let agent = flaky_agent(retrying(2, 1), provider.clone(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("whole"));
    assert_eq!(retries(&envelopes)[0].error["kind"], "transport");
    assert_eq!(
        messages(&*stores, "s1").await[1],
        linch_core::Message::assistant_text("whole")
    );
}

/// `max_attempts` bounds the calls to a model: the last failure ends the
/// run with its error, as a run without retries ends.
#[tokio::test]
async fn retries_stop_at_max_attempts_and_the_run_fails() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).down(MODEL));
    let stores = Arc::new(Stores::default());
    let agent = flaky_agent(retrying(3, 1), provider.clone(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(provider.calls().len(), 3);
    let numbers: Vec<_> = retries(&envelopes)
        .iter()
        .map(|r| (r.retry, r.delay_ms))
        .collect();
    assert_eq!(numbers, [(1, 1), (2, 2)], "the wait doubles");
    let error = envelopes.iter().find_map(|e| match &e.event {
        Event::Error(error) => Some(error.error.clone()),
        _ => None,
    });
    assert_eq!(error.unwrap()["status"], 503);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
}

/// A failure no retry can fix ends the run at once.
#[tokio::test]
async fn a_failure_that_is_not_retryable_ends_the_run_at_once() {
    let refused = ScriptedError::Status {
        status: 400,
        retry_after_ms: None,
        message: "bad request".into(),
    };
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, refused));
    let stores = Arc::new(Stores::default());
    let agent = flaky_agent(retrying(5, 1), provider.clone(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.calls().len(), 1);
    assert!(retries(&envelopes).is_empty());
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
}

/// A server's `Retry-After` is the wait, unjittered; one longer than
/// `max_delay_ms` ends the model's retries rather than hold the run.
#[tokio::test]
async fn a_retry_after_is_waited_and_one_past_the_cap_is_not() {
    let after = |ms| ScriptedError::Status {
        status: 429,
        retry_after_ms: Some(ms),
        message: "slow down".into(),
    };
    let mut spec = retrying(3, 1);
    spec.provider_retry.jitter = 1.0;
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, after(60)));
    let started = Instant::now();
    let agent = flaky_agent(spec.clone(), provider, Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(started.elapsed() >= Duration::from_millis(60));
    assert_eq!(retries(&envelopes)[0].delay_ms, 60);
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("done"));

    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, after(5_000)));
    let agent = flaky_agent(spec, provider.clone(), Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(retries(&envelopes).is_empty());
    assert_eq!(provider.calls().len(), 1);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
}

/// Each turn gets the whole retry budget again.
#[tokio::test]
async fn every_turn_has_its_own_retries() {
    let provider = Arc::new(
        Flaky::new(vec![tool_turn(0, 1, "Read"), text_turn("done")])
            .fail_with(0, MODEL, busy())
            .fail_with(1, MODEL, busy()),
    );
    let agent = flaky_agent(
        retrying(2, 1),
        provider.clone(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let numbers: Vec<_> = retries(&envelopes).iter().map(|r| r.retry).collect();
    assert_eq!(numbers, [1, 1]);
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("done"));
}

/// Read the stream until a `provider_retry` arrives.
async fn until_retry(stream: &mut linch_core::RunStream) {
    while let Some(item) = stream.next().await {
        if matches!(item.unwrap().event, Event::ProviderRetry(_)) {
            return;
        }
    }
    panic!("the run ended without a retry");
}

/// A cancel during the backoff ends the run `aborted` at once, without
/// waiting the backoff out or calling again.
#[tokio::test]
async fn a_cancel_during_the_backoff_aborts_at_once() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, busy()));
    let stores = Arc::new(Stores::default());
    let mut spec = retrying(3, 60_000);
    spec.provider_retry.max_delay_ms = 60_000;
    let cancel = CancellationToken::new();
    let mut options = RunOptions::new("s1");
    options.cancel = cancel.clone();
    let mut stream = flaky_agent(spec, provider.clone(), stores.clone()).run("go", options);
    let run_id = stream.run_id().clone();
    until_retry(&mut stream).await;
    cancel.cancel();
    let rest = tokio::time::timeout(Duration::from_secs(5), drain(stream)).await;
    let (envelopes, error) = rest.expect("the backoff ended with the cancel");
    assert!(error.is_none(), "{error:?}");
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Aborted);
    assert_eq!(provider.calls().len(), 1, "no call after the cancel");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Aborted);
}

/// A run dropped during its backoff, as a crash would drop it, resumes
/// with the call it was waiting to make, and its failed call stays
/// counted: one failed and one accepted receipt, one retry.
#[tokio::test]
async fn a_run_dropped_during_the_backoff_resumes_with_the_next_call() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, busy()));
    let stores = Arc::new(Stores::default());
    let mut spec = retrying(2, 60_000);
    spec.provider_retry.max_delay_ms = 60_000;
    let agent = flaky_agent(spec, provider.clone(), stores.clone());
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    until_retry(&mut stream).await;
    drop(stream);

    let resumed = tokio::time::timeout(
        Duration::from_secs(5),
        finish(agent.resume(run_id.clone(), RunOptions::new("s1"))),
    )
    .await
    .expect("the resume calls at once");
    assert_eq!(result(&resumed).final_text.as_deref(), Some("done"));
    assert_eq!(provider.calls().len(), 2);
    let events = durable(&stores, &run_id).await;
    assert_eq!(
        dispositions(&events),
        [CallDisposition::Failed, CallDisposition::Accepted]
    );
    assert_eq!(retries(&events).len(), 1);
    assert!(
        result(&resumed).usage_complete,
        "a 503 before any output cost nothing"
    );
}

/// The resumed run continues the turn's retries rather than start them
/// over: with two calls allowed and one spent, the resumed call is the
/// last.
#[tokio::test]
async fn a_resumed_run_does_not_start_the_turn_s_retries_over() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).down(MODEL));
    let stores = Arc::new(Stores::default());
    let mut spec = retrying(2, 60_000);
    spec.provider_retry.max_delay_ms = 60_000;
    let agent = flaky_agent(spec, provider.clone(), stores.clone());
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    until_retry(&mut stream).await;
    drop(stream);
    let resumed = finish(agent.resume(run_id, RunOptions::new("s1"))).await;
    assert_eq!(result(&resumed).subtype, ResultSubtype::Error);
    assert_eq!(provider.calls().len(), 2, "max_attempts across the crash");
}

/// Stores that keep every commit as written: its events, without their
/// times, its checkpoint and its status.
#[derive(Default)]
struct Recorder {
    stores: Stores,
    commits: Mutex<Vec<serde_json::Value>>,
}

#[async_trait]
impl RunStore for Recorder {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        RunStore::create(&self.stores, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.stores.claim(ns, run_id, owner).await
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&self.stores, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        let events: Vec<_> = (commit.events.iter())
            .map(|e| {
                let mut event = serde_json::to_value(&e.event).unwrap();
                event.as_object_mut().unwrap().remove("duration_ms");
                json!({"seq": e.seq, "event": event})
            })
            .collect();
        let checkpoint = serde_json::to_value(&commit.checkpoint).unwrap();
        let status = format!("{:?}", commit.status);
        let written = json!({"events": events, "checkpoint": checkpoint, "status": status});
        self.commits.lock().unwrap().push(written);
        self.stores.commit(lease, commit).await
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.stores.events(ns, run_id, after).await
    }
}

#[async_trait]
impl SessionStore for Recorder {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        SessionStore::create(&self.stores, ns, session).await
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&self.stores, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.stores.append(ns, session, expected, entries).await
    }
}

/// What one tool run of `spec` wrote, commit by commit with each
/// checkpoint, and what the provider was asked.
async fn trace(spec: AgentSpec) -> (Vec<serde_json::Value>, Vec<serde_json::Value>) {
    let provider = Arc::new(ScriptedProvider::new(script(1, 2, "Read")));
    let stores = Arc::new(Recorder::default());
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec, provider.clone(), tools, permissions, stores.clone());
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    finish(agent.run("go", options)).await;
    let requests = (provider.requests().iter())
        .map(|r| serde_json::to_value(r).unwrap())
        .collect();
    let commits = stores.commits.lock().unwrap().clone();
    assert!(commits.len() >= 5, "{commits:?}");
    (commits, requests)
}

/// With retries on (the default) and fallbacks set, a run with no failure
/// writes the same events, checkpoints and write groups, and sends the same
/// requests, as a run with retries off and no fallbacks.
#[tokio::test]
async fn reliability_costs_nothing_when_no_call_fails() {
    let plain = trace(retrying(1, 1)).await;
    let mut reliable = spec();
    reliable.fallback_models = vec!["fallback-model".into()];
    assert_eq!(trace(reliable).await, plain);
}

/// A spec that retries under a 1000-token cap.
fn capped() -> AgentSpec {
    let mut spec = retrying(3, 1);
    spec.budget = Some(linch_core::RunBudget {
        max_tokens: Some(1_000),
        ..Default::default()
    });
    spec
}

fn receipt_usages(envelopes: &[Envelope]) -> Vec<Option<linch_core::Usage>> {
    (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt.usage),
            _ => None,
        })
        .collect()
}

/// A provider that answered with an error status before any output, or a
/// connection never made, cost nothing: the receipt says so, the total stays
/// complete, and a capped budget lets the run retry.
#[tokio::test]
async fn under_a_capped_budget_a_call_known_to_cost_nothing_is_retried() {
    let refused = ScriptedError::Connect {
        message: "connection refused".into(),
    };
    for error in [busy(), refused] {
        let provider =
            Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, error.clone()));
        let stores = Arc::new(Stores::default());
        let agent = flaky_agent(capped(), provider.clone(), stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        let envelopes = finish(stream).await;
        let receipts = receipt_usages(&durable(&stores, &run_id).await);

        assert_eq!(provider.calls().len(), 2, "{error:?}");
        assert_eq!(retries(&envelopes).len(), 1, "{error:?}");
        assert_eq!(receipts[0], Some(linch_core::Usage::default()), "{error:?}");
        let done = result(&envelopes);
        assert_eq!(done.subtype, ResultSubtype::Success, "{error:?}");
        assert!(done.usage_complete, "{error:?}");
        let usage_events = (envelopes.iter()).filter(|e| matches!(e.event, Event::Usage(_)));
        assert_eq!(
            usage_events.count(),
            1,
            "no usage event for the free failure"
        );
    }
}

/// A request that went out with nothing back (a reset or a read timeout),
/// or a failure after output began, may have been paid: its usage stays
/// unknown, and a capped budget ends the run with the provider's error.
#[tokio::test]
async fn under_a_capped_budget_a_call_that_may_have_been_paid_ends_the_run() {
    let reset = ScriptedTurn::failing(ScriptedError::Transport {
        message: "connection reset".into(),
    });
    let mut cut = TurnBuilder::new(MODEL).text("partial").build();
    cut.events.pop();
    cut.error = Some(busy());
    for (failure, kind) in [(reset, "transport"), (cut, "status")] {
        let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail(0, MODEL, failure));
        let stores = Arc::new(Stores::default());
        let agent = flaky_agent(capped(), provider.clone(), stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        let envelopes = finish(stream).await;
        let receipts = receipt_usages(&durable(&stores, &run_id).await);

        assert_eq!(provider.calls().len(), 1, "{kind}");
        assert!(retries(&envelopes).is_empty(), "{kind}");
        assert_eq!(receipts, [None], "{kind}");
        let error = envelopes.iter().find_map(|e| match &e.event {
            Event::Error(error) => Some(error.error.clone()),
            _ => None,
        });
        assert_eq!(error.unwrap()["kind"], kind);
        assert!(!result(&envelopes).usage_complete, "{kind}");
    }
}

/// A spec that says nothing retries with the defaults: the first 429 does
/// not end the run.
#[tokio::test]
async fn a_spec_that_says_nothing_retries() {
    let limited = ScriptedError::Status {
        status: 429,
        retry_after_ms: Some(1),
        message: "slow down".into(),
    };
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, limited));
    let agent = flaky_agent(spec(), provider.clone(), Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.calls().len(), 2);
    assert_eq!(retries(&envelopes).len(), 1);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Success);
}

/// One attempt turns retries off: a failure ends the run at once.
#[tokio::test]
async fn one_attempt_turns_retries_off() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, busy()));
    let agent = flaky_agent(
        retrying(1, 1),
        provider.clone(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.calls().len(), 1);
    assert!(retries(&envelopes).is_empty());
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
}
