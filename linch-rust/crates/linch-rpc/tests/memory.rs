//! Long-term `memory`: a tenant needs an explicit allowance, its cap on
//! memories bounds what a config asks for, and a background agent (a job
//! no callback can serve) saves a memory in one run that a later run
//! recalls. The Postgres test skips without `LINCH_TEST_PG_DSN`.

mod common;

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::{Envelope, Event, PermissionMode, RunId};
use linch_rpc::protocol::{Events, JobInfo, method};
use linch_rpc::{Allowlists, BackgroundAgents, Tenant};
use linch_worker::{Worker, WorkerConfig};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

/// A scripted agent with `memory` that may write memories unasked
/// (`accept_edits`, which a background run needs), in namespace `namespace`.
fn config(turns: Vec<linch::ScriptedTurn>, namespace: &str, root: &Path, memory: Value) -> Value {
    let mut config = scripted(turns, None, "");
    config["namespace"] = json!(namespace);
    config["memory"] = memory;
    config["permissions"] = json!({"mode": "accept_edits", "project_root": root});
    config
}

fn allow(root: &Path) -> Allowlists {
    let modes = [PermissionMode::Default, PermissionMode::AcceptEdits];
    Allowlists::new()
        .scripted_provider(true)
        .permission_modes(&modes)
        .workspace_roots(&[root.to_path_buf()])
        .unwrap()
}

/// The text and error code of each tool result, in order.
fn results(envelopes: &[Envelope]) -> Vec<(Option<String>, String)> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => {
                let result = serde_json::to_value(&end.result).unwrap();
                let text = result["content"][0]["text"].as_str().unwrap_or("");
                Some((end.error_code.clone(), text.to_owned()))
            }
            _ => None,
        })
        .collect()
}

/// `metadata.memory` of each `context_build` event.
fn recalled(envelopes: &[Envelope]) -> Vec<Value> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ContextBuild(build) => build.metadata.get("memory").cloned(),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn a_tenant_needs_the_allowance_and_local_clients_do_not() {
    let dir = tempfile::tempdir().unwrap();
    let on = config(vec![text_turn("ok")], "acme", dir.path(), json!({}));

    // Deny by default.
    let rpc = Rpc::remote(tenant("acme", allow(dir.path())));
    assert_eq!(refused(&rpc, on.clone()).await.as_deref(), Some("memory"));
    let mut off = on.clone();
    off.as_object_mut().unwrap().remove("memory");
    rpc.open(off).await;

    // Allowed.
    let rpc = Rpc::remote(tenant("acme", allow(dir.path()).memory(true)));
    assert_eq!(refused(&rpc, on.clone()).await, None);

    // A local client has no tenant to hold it.
    let mut local = on;
    local["namespace"] = json!("rpc");
    assert_eq!(refused(&Rpc::new(), local).await, None);
}

/// The agent's own `max_memories` and the tenant's: the smaller holds.
#[tokio::test]
async fn the_tenants_cap_bounds_the_config() {
    let dir = tempfile::tempdir().unwrap();
    let save = |id: &str, text: &str| tool_turn(id, "MemorySave", json!({"content": text}));
    let turns = || {
        vec![
            save("c1", "one"),
            save("c2", "two"),
            save("c3", "three"),
            text_turn("done"),
        ]
    };
    let allowed = || allow(dir.path()).memory(true).max_memories(Some(2));
    for asked in [
        json!({}),
        json!({"max_memories": 5}),
        json!({"max_memories": 1}),
    ] {
        let mut rpc = Rpc::remote(tenant("acme", allowed()));
        let config = config(turns(), "acme", dir.path(), asked.clone());
        let agent = rpc.open(config).await;
        let run_id = rpc.start(&agent, "s", "remember").await;
        let ended = rpc.until_end(&run_id).await;
        let codes: Vec<Option<String>> = results(&ended.envelopes)
            .into_iter()
            .map(|(code, _)| code)
            .collect();
        let limit = Some("limit_reached".to_owned());
        let expected = match asked.get("max_memories") {
            Some(cap) if cap == &json!(1) => vec![None, limit.clone(), limit],
            _ => vec![None, None, limit],
        };
        assert_eq!(codes, expected, "asked {asked}");
    }
}

fn worker_config(owner: &str) -> WorkerConfig {
    let mut config = WorkerConfig::new(owner);
    config.poll_interval = Duration::from_millis(50);
    config.retry_delay = Duration::from_millis(100);
    config
}

async fn finished(rpc: &Rpc, agent: &str, job_id: i64) -> JobInfo {
    for _ in 0..1200 {
        let got = rpc
            .call(method::JOB_GET, json!({"agent": agent, "job_id": job_id}))
            .await
            .unwrap();
        let job: JobInfo = serde_json::from_value(got).unwrap();
        if job.state == "done" || job.state == "failed" {
            return job;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("job {job_id} never finished");
}

async fn events(rpc: &Rpc, agent: &str, run_id: &RunId) -> Vec<Envelope> {
    let params = json!({"agent": agent, "run_id": run_id});
    let events = rpc.call(method::RUN_EVENTS, params).await.unwrap();
    serde_json::from_value::<Events>(events).unwrap().events
}

/// A queued job whose agent has no callbacks and no host tools saves a
/// memory; a later job of another agent of the namespace, in another
/// session, recalls it. A worker runs both, with nothing to call back.
#[tokio::test]
async fn a_background_run_saves_a_memory_and_a_later_run_recalls_it() {
    let Some(db) = TestDb::empty("memory_jobs").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = (allow(dir.path()).memory(true))
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
        .max_queued_jobs(Some(4))
        .max_memories(Some(10));
    let acme = Arc::new(Tenant::new("acme", allowed).unwrap());
    let rpc = Rpc::remote(linch_rpc::Access::Tenant(
        Arc::clone(&acme).connect().unwrap(),
    ));
    let on_pg = |mut config: Value| {
        config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
        config
    };
    let memory = json!({"recall": {"k": 3}});
    let writer_turns = vec![
        tool_turn(
            "c1",
            "MemorySave",
            json!({"content": "The nightly report goes to ops@example.com", "tags": ["report"]}),
        ),
        text_turn("noted"),
    ];
    let reader_turns = vec![text_turn("to ops")];
    let writer = rpc
        .open(on_pg(config(
            writer_turns,
            "acme",
            dir.path(),
            memory.clone(),
        )))
        .await;
    let reader = rpc
        .open(on_pg(config(reader_turns, "acme", dir.path(), memory)))
        .await;

    let enqueue = |agent: &str, session: &str, prompt: &str| {
        let params = json!({"agent": agent, "session_id": session, "prompt": prompt});
        rpc.call(method::JOB_ENQUEUE, params)
    };
    let saved = enqueue(&writer, "nightly", "Remember where the report goes.").await;
    let saved: JobInfo = serde_json::from_value(saved.unwrap()).unwrap();

    let pool = linch::postgres::connect(
        &db.dsn,
        linch::postgres::CONNECT_TIMEOUT,
        4.try_into().unwrap(),
    )
    .await
    .unwrap();
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::clone(&acme));
    let shutdown = CancellationToken::new();
    let worker = Worker::new(pool.clone(), worker_config("w1"))
        .definitions(factory.roots(), Arc::new(factory));
    let running = tokio::spawn(worker.run(shutdown.clone()));

    let saved = finished(&rpc, &writer, saved.job_id).await;
    assert_eq!(saved.state, "done", "{saved:?}");
    let run = events(&rpc, &writer, &saved.run_id.expect("the job names its run")).await;
    let said = results(&run);
    assert!(
        said[0].0.is_none() && said[0].1.starts_with("Saved memory "),
        "{said:?}"
    );

    let asked = enqueue(&reader, "morning", "Where does the report go?").await;
    let asked: JobInfo = serde_json::from_value(asked.unwrap()).unwrap();
    let asked = finished(&rpc, &reader, asked.job_id).await;
    assert_eq!(asked.state, "done", "{asked:?}");
    let run = events(&rpc, &reader, &asked.run_id.expect("the job names its run")).await;
    let ids = recalled(&run);
    assert_eq!(ids.len(), 1, "recall ran once: {ids:?}");
    let id = said[0].1.strip_prefix("Saved memory ").unwrap();
    assert_eq!(ids[0]["ids"], json!([id]));

    shutdown.cancel();
    running.await.unwrap().unwrap();
    // The table holds the one row, in the tenant's namespace.
    let rows: Vec<(String, Option<String>)> =
        sqlx::query_as("SELECT namespace, scope FROM linch_memories")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(rows, [("acme".to_owned(), None)]);
    db.drop().await;
}
