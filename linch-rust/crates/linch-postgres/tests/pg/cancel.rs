//! `Agent::cancel` on Postgres: each agent handle stands for one server
//! process, with its own pool and `PgRunStore` (its own lease holder). A
//! run suspended by one process is cancelled by another after a restart;
//! a cancel that fails part way releases its lease, so the next one goes
//! on at once; a cancel that races a live approve elsewhere is refused.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::events::kinds::ResultSubtype;
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Event, JsonObject, PermissionEngine, PermissionMode,
    PermissionPolicy, RunError, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider,
    SessionId, SessionStore, StoreError, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult,
    ToolScope,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::{Notify, Semaphore};

use crate::common::TestDb;

const MODEL: &str = "scripted-model";

/// A write tool that waits at a gate.
struct Deploy {
    schema: JsonObject,
    entered: Notify,
    open: Semaphore,
}

#[async_trait]
impl Tool for Deploy {
    fn name(&self) -> &str {
        "deploy"
    }
    fn description(&self) -> &str {
        "Deploy."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        self.entered.notify_one();
        self.open.acquire().await.expect("never closed").forget();
        Ok(ToolOutput::new("deployed"))
    }
}

/// One process: a pool, a lease holder and an agent; its model asks to
/// deploy, then answers, or (`answering`) only answers.
struct Process {
    agent: Agent,
    tool: Arc<Deploy>,
}

impl Process {
    async fn new(db: &TestDb, faults: Option<u64>, answering: bool) -> Self {
        let pool = PgPoolOptions::new()
            .max_connections(3)
            .connect_with(db.options.clone())
            .await
            .expect("connect");
        let call = TurnBuilder::new(MODEL)
            .tool_call("c1", "deploy", &serde_json::json!({}))
            .build();
        let answer = TurnBuilder::new(MODEL).text("done").build();
        let turns = match answering {
            true => vec![answer],
            false => vec![call, answer],
        };
        let provider = Arc::new(ScriptedProvider::new(turns));
        let mut schema = JsonObject::new();
        schema.insert("type".into(), "object".into());
        let tool = Arc::new(Deploy {
            schema,
            entered: Notify::new(),
            open: Semaphore::new(0),
        });
        let tools = ToolRegistry::new();
        let _ = tools.register(Arc::clone(&tool) as Arc<dyn Tool>).unwrap();
        let runs: Arc<dyn RunStore> = match faults {
            Some(n) => {
                let store = Arc::new(PgRunStore::new(pool.clone()));
                Arc::new(Faulty::new(store, n, FaultTiming::Before))
            }
            None => Arc::new(PgRunStore::new(pool.clone())),
        };
        let policy = PermissionPolicy::new(PermissionMode::Default, "/");
        let agent = AgentBuilder::new(AgentSpec::new(MODEL))
            .provider(provider)
            .tools(Arc::new(tools))
            .permissions(PermissionEngine::new(policy).unwrap())
            .run_store(runs)
            .session_store(Arc::new(PgSessionStore::new(pool)))
            .namespace("ns")
            .build()
            .unwrap();
        Self { agent, tool }
    }
}

/// Start a run on `process` until it suspends for the deploy's approval.
async fn suspend(process: &Process) -> RunId {
    let stream = process.agent.run("deploy", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let items: Vec<_> = stream.collect().await;
    let last = items.last().unwrap().as_ref().unwrap();
    assert!(matches!(last.event, Event::Suspended(_)), "{last:?}");
    run_id
}

fn allow() -> HashMap<String, ApprovalResponse> {
    HashMap::from([("c1".to_owned(), ApprovalResponse::Allow)])
}

async fn status(db: &TestDb, run_id: &RunId) -> RunStatus {
    let store = PgRunStore::new(db.pool.clone());
    store.load("ns", run_id).await.unwrap().status
}

/// The run's session: its prefix, the prompt, the call, and the call's
/// aborted result.
async fn assert_paired(db: &TestDb) {
    let sessions = PgSessionStore::new(db.pool.clone());
    let entries = sessions.load("ns", &SessionId::new("s1")).await.unwrap();
    assert_eq!(entries.len(), 4, "{entries:?}");
    let text = format!("{:?}", entries.last());
    assert!(text.contains("Tool call aborted"), "{text}");
}

/// A run suspended by a process that is gone is cancelled by another; a
/// late approve is refused, and a second cancel is a no-op.
#[tokio::test]
async fn a_suspended_run_is_cancelled_after_a_restart() {
    let Some(db) = TestDb::migrated("cancel_restart").await else {
        return;
    };
    let run_id = suspend(&Process::new(&db, None, false).await).await;
    let b = Process::new(&db, None, true).await;
    assert!(
        b.agent
            .cancel(run_id.clone(), RunOptions::new(""))
            .await
            .unwrap()
    );
    assert_eq!(status(&db, &run_id).await, RunStatus::Aborted);
    assert_paired(&db).await;

    let items: Vec<_> = (b.agent)
        .approve(run_id.clone(), allow(), RunOptions::new(""))
        .collect()
        .await;
    assert!(
        matches!(items[..], [Err(RunError::Terminal { .. })]),
        "{items:?}"
    );
    assert!(
        !b.agent
            .cancel(run_id.clone(), RunOptions::new(""))
            .await
            .unwrap()
    );
    // The session takes a new run.
    let next: Vec<_> = (b.agent)
        .run("again", RunOptions::new("s1"))
        .collect()
        .await;
    assert!(matches!(next.last(), Some(Ok(_))), "{next:?}");
    db.finish().await;
}

/// A cancel whose terminal write fails leaves its intent durable and its
/// lease released: another process finishes the abort at once, without
/// waiting out the 30 s lease.
#[tokio::test]
async fn a_failed_cancel_releases_its_lease() {
    let Some(db) = TestDb::migrated("cancel_release").await else {
        return;
    };
    let run_id = suspend(&Process::new(&db, None, false).await).await;
    // Run writes: the claim, the `Aborting` commit, then the terminal one.
    let crashing = Process::new(&db, Some(3), true).await;
    let error = crashing
        .agent
        .cancel(run_id.clone(), RunOptions::new(""))
        .await;
    assert!(error.is_err(), "{error:?}");
    crashing.agent.released().await;
    assert_eq!(status(&db, &run_id).await, RunStatus::Running);

    let c = Process::new(&db, None, true).await;
    let ended = tokio::time::timeout(
        Duration::from_secs(5),
        c.agent.cancel(run_id.clone(), RunOptions::new("")),
    )
    .await
    .expect("not held by a lease");
    assert!(ended.unwrap());
    assert_eq!(status(&db, &run_id).await, RunStatus::Aborted);
    assert_paired(&db).await;
    db.finish().await;
}

/// A cancel of a run another process approved and drives loses and writes
/// nothing: `false` once the approve committed its decisions (the run is
/// no longer suspended), `RunLeased` while the approve's claim is all
/// there is. The approve completes the run.
#[tokio::test]
async fn a_cancel_racing_a_live_approve_is_refused() {
    let Some(db) = TestDb::migrated("cancel_race").await else {
        return;
    };
    let run_id = suspend(&Process::new(&db, None, false).await).await;
    let b = Process::new(&db, None, true).await;
    let mut approve = b
        .agent
        .approve(run_id.clone(), allow(), RunOptions::new(""));
    let polled = async {
        while let Some(item) = approve.next().await {
            item.expect("the approve goes on until the tool");
        }
    };
    tokio::select! {
        () = polled => panic!("the tool waits"),
        () = b.tool.entered.notified() => {}
    }
    let c = Process::new(&db, None, true).await;
    let before = PgRunStore::new(db.pool.clone()).load("ns", &run_id).await;
    let refused = c.agent.cancel(run_id.clone(), RunOptions::new("")).await;
    assert!(
        matches!(
            refused,
            Ok(false) | Err(RunError::Store(StoreError::RunLeased { .. }))
        ),
        "{refused:?}"
    );
    let after = PgRunStore::new(db.pool.clone()).load("ns", &run_id).await;
    let (before, after) = (before.unwrap(), after.unwrap());
    assert_eq!(
        (before.epoch, before.last_seq),
        (after.epoch, after.last_seq)
    );
    b.tool.open.add_permits(1);
    let rest: Vec<_> = approve.collect().await;
    let last = rest.last().unwrap().as_ref().unwrap();
    assert!(
        matches!(&last.event, Event::Result(r) if r.subtype == ResultSubtype::Success),
        "{last:?}"
    );
    assert_eq!(status(&db, &run_id).await, RunStatus::Completed);
    db.finish().await;
}

/// A steer of a run another process drives is refused with `RunLeased`,
/// which names the holder and how long its lease runs, and writes nothing.
#[tokio::test]
async fn a_steer_of_a_run_another_process_drives_names_its_holder() {
    let Some(db) = TestDb::migrated("steer_leased").await else {
        return;
    };
    let run_id = suspend(&Process::new(&db, None, false).await).await;
    let b = Process::new(&db, None, true).await;
    let mut approve = b
        .agent
        .approve(run_id.clone(), allow(), RunOptions::new(""));
    let polled = async {
        while let Some(item) = approve.next().await {
            item.expect("the approve goes on until the tool");
        }
    };
    tokio::select! {
        () = polled => panic!("the tool waits"),
        () = b.tool.entered.notified() => {}
    }
    let c = Process::new(&db, None, true).await;
    let before = PgRunStore::new(db.pool.clone()).load("ns", &run_id).await;
    let refused = c.agent.steer(run_id.clone(), "use tabs", None).await;
    match refused {
        Err(RunError::Store(StoreError::RunLeased {
            owner, expires_in, ..
        })) => {
            assert!(!owner.is_empty());
            assert!(expires_in > Duration::ZERO, "{expires_in:?}");
        }
        other => panic!("{other:?}"),
    }
    let after = PgRunStore::new(db.pool.clone()).load("ns", &run_id).await;
    let (before, after) = (before.unwrap(), after.unwrap());
    assert_eq!(
        (before.epoch, before.last_seq),
        (after.epoch, after.last_seq)
    );
    b.tool.open.add_permits(1);
    let rest: Vec<_> = approve.collect().await;
    assert!(rest.last().unwrap().is_ok());
    db.finish().await;
}
