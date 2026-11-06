//! A subagent's child run never outlives the parent's interest in it: a
//! call that the call timeout or the parent's cancel cuts short leaves its
//! child run aborted, durably, by the time the parent's stream ends.

mod common;

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use common::subagents::*;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::{
    AgentBuilder, Commit, Envelope, Event, Lease, NewRun, PermissionMode, Provider, ProviderError,
    ProviderRequest, ProviderStream, RunId, RunOptions, RunRecord, RunStatus, RunStore, StoreError,
    ToolScope,
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

/// Answers the parent like [`Family`]; each child's call waits until it
/// is cancelled, after telling `started`.
struct Stall {
    family: Family,
    started: Arc<Notify>,
}

#[async_trait]
impl Provider for Stall {
    fn id(&self) -> &str {
        "stall"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if who(&request) == "parent" {
            return self.family.stream(request, cancel).await;
        }
        self.started.notify_one();
        cancel.cancelled().await;
        Err(ProviderError::Cancelled)
    }
}

fn stall() -> Arc<Stall> {
    let parent = vec![spawn_turn(None, &["check x"]), text_turn("done")];
    Arc::new(Stall {
        family: Family::new(parent, Vec::new()),
        started: Arc::new(Notify::new()),
    })
}

/// Stores whose child-run commits (a child's id holds a `:`) take
/// 300 ms, so a child's abort ends well after its parent's own writes:
/// unless the parent waits for it, the parent ends first, and a dropped
/// child stream never finishes the abort at all.
struct SlowChildren(Arc<Stores>);

#[async_trait]
impl RunStore for SlowChildren {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        RunStore::create(&*self.0, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.0.claim(ns, run_id, owner).await
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&*self.0, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        if lease.run_id.as_str().contains(':') {
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        self.0.commit(lease, commit).await
    }
    async fn events(&self, ns: &str, id: &RunId, after: u64) -> Result<Vec<Envelope>, StoreError> {
        self.0.events(ns, id, after).await
    }
}

fn agent(provider: Arc<Stall>, stores: &Arc<Stores>, timeout_ms: Option<u64>) -> linch_core::Agent {
    let runs = Runs::default();
    let mut spec = spec();
    spec.call_timeout_ms = timeout_ms;
    let slow = Arc::new(SlowChildren(stores.clone()));
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(slow)
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap()
}

/// The child run's status and how its `Result` ended.
async fn child_end(stores: &Stores, child: &RunId) -> (RunStatus, Option<ResultSubtype>) {
    let record = stores.runs.load(NS, child).await.unwrap();
    let events = stores.runs.events(NS, child, 0).await.unwrap();
    let ended = events.iter().find_map(|envelope| match &envelope.event {
        Event::Result(result) => Some(result.subtype),
        _ => None,
    });
    (record.status, ended)
}

#[tokio::test]
async fn a_subagent_call_cut_by_its_timeout_aborts_the_child_run() {
    let stores = Arc::new(Stores::default());
    let provider = stall();
    let agent = agent(provider.clone(), &stores, Some(500));
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        ["Tool \"Subagent\" timed out after 500ms"]
    );
    assert_eq!(log.last().unwrap().text(), "done", "the parent went on");
    let child = RunId::new(format!("{run_id}:sub_0"));
    let end = child_end(&stores, &child).await;
    assert_eq!(end, (RunStatus::Aborted, Some(ResultSubtype::Aborted)));
    let last = envelopes.last().unwrap();
    assert!(matches!(&last.event, Event::Result(r) if r.subtype == ResultSubtype::Success));
}

#[tokio::test]
async fn a_cancelled_parent_aborts_its_subagent_before_it_ends() {
    let stores = Arc::new(Stores::default());
    let provider = stall();
    let agent = agent(provider.clone(), &stores, None);
    let cancel = CancellationToken::new();
    let mut options = RunOptions::new("s1");
    options.cancel = cancel.clone();
    let stream = agent.run("go", options);
    let run_id = stream.run_id().clone();
    let started = provider.started.clone();
    let canceller = tokio::spawn(async move {
        started.notified().await;
        cancel.cancel();
    });
    let envelopes = finish(stream).await;
    canceller.await.unwrap();

    let last = envelopes.last().unwrap();
    assert!(matches!(&last.event, Event::Result(r) if r.subtype == ResultSubtype::Aborted));
    let child = RunId::new(format!("{run_id}:sub_0"));
    let end = child_end(&stores, &child).await;
    assert_eq!(end, (RunStatus::Aborted, Some(ResultSubtype::Aborted)));
}
