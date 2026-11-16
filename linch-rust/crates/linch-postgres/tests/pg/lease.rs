//! Two agent handles on one database stand for two server processes: one
//! drives a run, the other tries to resume it.

use std::sync::Arc;
use std::time::Instant;

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, PermissionEngine, PermissionMode,
    PermissionPolicy, Provider, RunError, RunOptions, RunStore, ScriptedProvider,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::{Notify, Semaphore};

use crate::common::TestDb;
use crate::idle::Gated;

const MODEL: &str = "scripted-model";

/// A pool of its own, as another process would have.
async fn process_pool(db: &TestDb) -> PgPool {
    PgPoolOptions::new()
        .max_connections(3)
        .connect_with(db.options.clone())
        .await
        .expect("connect")
}

fn agent(provider: Arc<dyn Provider>, runs: Arc<dyn RunStore>, pool: &PgPool) -> Agent {
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(runs)
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace("ns")
        .build()
        .unwrap()
}

fn gated(text: &str) -> Arc<Gated> {
    Arc::new(Gated {
        inner: ScriptedProvider::new([TurnBuilder::new(MODEL).text(text).build()]),
        entered: Notify::new(),
        open: Semaphore::new(0),
    })
}

fn scripted(text: &str) -> Arc<ScriptedProvider> {
    Arc::new(ScriptedProvider::new([TurnBuilder::new(MODEL)
        .text(text)
        .build()]))
}

fn outcome(items: &[Result<Envelope, RunError>]) -> String {
    match items.last() {
        Some(Ok(envelope)) => match &envelope.event {
            Event::Result(result) => format!("result {:?}", result.subtype),
            other => format!("ended on {other:?}"),
        },
        Some(Err(error)) => format!("error {error:?}"),
        None => "no item".into(),
    }
}

/// B resumes while A's model call is in flight. B is refused and writes
/// nothing, so the call is paid once and A's run completes.
#[tokio::test]
async fn a_live_holder_keeps_its_run() {
    let Some(db) = TestDb::migrated("lease_live").await else {
        return;
    };
    let (pool_a, pool_b) = (process_pool(&db).await, process_pool(&db).await);
    let provider_a = gated("from a");
    let provider_b = scripted("from b");
    let agent_a = agent(
        provider_a.clone(),
        Arc::new(PgRunStore::new(pool_a.clone())),
        &pool_a,
    );
    let agent_b = agent(
        provider_b.clone(),
        Arc::new(PgRunStore::new(pool_b.clone())),
        &pool_b,
    );
    let stream_a = agent_a.run("hi", RunOptions::new("s1"));
    let run_id = stream_a.run_id().clone();
    let task_a = tokio::spawn(stream_a.collect::<Vec<_>>());
    provider_a.entered.notified().await;
    let items_b: Vec<_> = agent_b
        .resume(run_id.clone(), RunOptions::new("s1"))
        .collect()
        .await;
    assert_eq!(items_b.len(), 1, "B gets one error: {}", outcome(&items_b));
    assert!(
        outcome(&items_b).contains("held by"),
        "B is refused: {}",
        outcome(&items_b)
    );
    let store = PgRunStore::new(db.pool.clone());
    assert_eq!(store.load("ns", &run_id).await.unwrap().epoch, 1, "B wrote nothing");
    provider_a.open.add_permits(1);
    let items_a = task_a.await.unwrap();
    assert_eq!(outcome(&items_a), "result Success");
    assert_eq!(provider_a.inner.requests().len(), 1);
    assert_eq!(provider_b.requests().len(), 0, "the call is paid once");
    pool_a.close().await;
    pool_b.close().await;
    db.finish().await;
}

/// STEP 1 observation: A's process is gone (its stream is leaked, never
/// polled or dropped); how soon can B take the run over?
#[tokio::test]
async fn observe_a_second_handle_resuming_a_dead_holders_run() {
    let Some(db) = TestDb::migrated("lease_observe_dead").await else {
        return;
    };
    let (pool_a, pool_b) = (process_pool(&db).await, process_pool(&db).await);
    let provider_a = gated("from a");
    let provider_b = scripted("from b");
    let agent_a = agent(
        provider_a.clone(),
        Arc::new(PgRunStore::new(pool_a.clone())),
        &pool_a,
    );
    let agent_b = agent(
        provider_b.clone(),
        Arc::new(PgRunStore::new(pool_b.clone())),
        &pool_b,
    );
    let mut stream_a = agent_a.run("hi", RunOptions::new("s1"));
    let run_id = stream_a.run_id().clone();
    let polled = async {
        while stream_a.next().await.is_some() {}
    };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = provider_a.entered.notified() => {}
    }
    // A crash: nothing of A runs again, nothing is cleaned up.
    std::mem::forget(stream_a);
    let started = Instant::now();
    let items_b: Vec<_> = agent_b
        .resume(run_id.clone(), RunOptions::new("s1"))
        .collect()
        .await;
    eprintln!(
        "B after {:?}: {}",
        started.elapsed(),
        outcome(&items_b)
    );
    pool_b.close().await;
    db.finish().await;
}
