//! The scheduler methods: schedules and background runs of an open agent,
//! the refusals, the tenant caps, and a worker that runs them through
//! `BackgroundAgents`. The Postgres tests skip without `LINCH_TEST_PG_DSN`.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::Event;
use linch_rpc::protocol::{Events, JobInfo, ScheduleInfo, Schedules, method};
use linch_rpc::{Access, Allowlists, BackgroundAgents, Tenant, code};
use linch_worker::{Worker, WorkerConfig};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

fn on_postgres(db: &TestDb, namespace: &str) -> Value {
    let mut config = scripted(vec![text_turn("checked in")], None, "");
    config["namespace"] = json!(namespace);
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

fn allow(db: &TestDb) -> Allowlists {
    Allowlists::new()
        .scripted_provider(true)
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
}

fn info(value: Value) -> ScheduleInfo {
    serde_json::from_value(value).unwrap()
}

fn job(value: Value) -> JobInfo {
    serde_json::from_value(value).unwrap()
}

#[tokio::test]
async fn schedules_are_created_listed_paused_resumed_and_deleted() {
    let Some(db) = TestDb::empty("sched_local").await else {
        return;
    };
    let rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db, "rpc")).await;
    let created = rpc
        .call(
            method::SCHEDULE_CREATE,
            json!({"agent": agent, "id": "digest", "prompt": "Write the digest.",
                   "cron": "0 7 * * *", "timezone": "Europe/Berlin"}),
        )
        .await
        .unwrap();
    let created = info(created);
    assert_eq!(
        (
            created.id.as_str(),
            created.namespace.as_str(),
            created.queue.as_str()
        ),
        ("digest", "rpc", "default")
    );
    assert_eq!(created.timezone.as_deref(), Some("Europe/Berlin"));
    assert_eq!(
        (created.paused, created.fires, created.session_id),
        (false, 0, None)
    );
    // A heartbeat: a fixed session and an interval; a fresh id.
    let beat = rpc
        .call(
            method::SCHEDULE_CREATE,
            json!({"agent": agent, "prompt": "Check in.", "interval_ms": 3_600_000,
                   "session_id": "ops"}),
        )
        .await
        .unwrap();
    let beat = info(beat);
    assert_eq!(beat.interval_ms, Some(3_600_000));
    assert_eq!(beat.session_id.as_ref().map(|s| s.as_str()), Some("ops"));
    assert_eq!(beat.id.len(), 36, "a uuid");

    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    let listed: Schedules = serde_json::from_value(listed).unwrap();
    assert_eq!(listed.schedules.len(), 2);
    let id = json!({"agent": agent, "id": "digest"});
    let paused = rpc.call(method::SCHEDULE_PAUSE, id.clone()).await.unwrap();
    assert!(info(paused).paused);
    let resumed = rpc.call(method::SCHEDULE_RESUME, id.clone()).await.unwrap();
    assert!(!info(resumed).paused);
    let deleted = rpc.call(method::SCHEDULE_DELETE, id.clone()).await.unwrap();
    assert_eq!(deleted, json!({"deleted": true}));
    let again = rpc.call(method::SCHEDULE_DELETE, id.clone()).await.unwrap();
    assert_eq!(again, json!({"deleted": false}));
    let missing = rpc.call(method::SCHEDULE_PAUSE, id).await.unwrap_err();
    assert_eq!(code_of(missing), code::SCHEDULE_NOT_FOUND);

    // A one-off background run, queued until a worker takes it.
    let queued = rpc
        .call(
            method::JOB_ENQUEUE,
            json!({"agent": agent, "session_id": "s1", "prompt": "go"}),
        )
        .await
        .unwrap();
    let queued = job(queued);
    assert_eq!((queued.state.as_str(), queued.attempts), ("queued", 0));
    let got = rpc
        .call(
            method::JOB_GET,
            json!({"agent": agent, "job_id": queued.job_id}),
        )
        .await
        .unwrap();
    assert_eq!(job(got), queued);
    let listed = rpc
        .call(method::JOB_LIST, json!({"agent": agent, "limit": 5}))
        .await
        .unwrap();
    assert_eq!(listed["jobs"].as_array().unwrap().len(), 1);
    let unknown = rpc
        .call(method::JOB_GET, json!({"agent": agent, "job_id": 999_999}))
        .await
        .unwrap_err();
    assert_eq!(code_of(unknown), code::JOB_NOT_FOUND);

    // Another namespace on the same database sees none of them.
    let other = rpc.open(on_postgres(&db, "other")).await;
    let none = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": other}))
        .await
        .unwrap();
    assert_eq!(none, json!({"schedules": []}));
    let hidden = rpc
        .call(
            method::JOB_GET,
            json!({"agent": other, "job_id": queued.job_id}),
        )
        .await
        .unwrap_err();
    assert_eq!(code_of(hidden), code::JOB_NOT_FOUND);
    db.drop().await;
}

#[tokio::test]
async fn an_agent_that_cannot_run_in_the_background_is_refused() {
    let Some(db) = TestDb::empty("sched_refused").await else {
        return;
    };
    let rpc = Rpc::new();
    let reason = |error| match error {
        linch_rpc::ClientError::Rpc(error) => (error.code, error.data.unwrap()["reason"].clone()),
        other => panic!("{other:?}"),
    };
    let memory = rpc.open(scripted(vec![], None, "")).await;
    let create = |agent: &str| json!({"agent": agent, "prompt": "p", "interval_ms": 60_000});
    let refused = rpc.call(method::SCHEDULE_CREATE, create(&memory)).await;
    assert_eq!(
        reason(refused.unwrap_err()),
        (code::NOT_BACKGROUND, json!("memory_store"))
    );
    let mut hosted_config = on_postgres(&db, "rpc");
    hosted_config["tools"] = json!([host_tool("Lookup", "read")]);
    let hosted = rpc.open(hosted_config).await;
    let refused = rpc
        .call(
            method::JOB_ENQUEUE,
            json!({"agent": hosted, "session_id": "s", "prompt": "p"}),
        )
        .await;
    assert_eq!(
        reason(refused.unwrap_err()),
        (code::NOT_BACKGROUND, json!("host_tools"))
    );

    let agent = rpc.open(on_postgres(&db, "rpc")).await;
    for bad in [
        json!({"agent": agent, "prompt": "p"}),
        json!({"agent": agent, "prompt": "p", "cron": "* * * * *", "interval_ms": 1000}),
        json!({"agent": agent, "prompt": "p", "interval_ms": 1000, "timezone": "UTC"}),
        json!({"agent": agent, "prompt": "p", "cron": "61 * * * *"}),
        json!({"agent": agent, "prompt": "p", "cron": "0 9 * * *", "timezone": "Mars/Base"}),
        json!({"agent": agent, "prompt": "p", "interval_ms": 0}),
    ] {
        let refused = rpc.call(method::SCHEDULE_CREATE, bad.clone()).await;
        assert_eq!(
            code_of(refused.unwrap_err()),
            code::INVALID_SCHEDULE,
            "{bad}"
        );
    }
    let once = json!({"agent": agent, "id": "a", "prompt": "p", "interval_ms": 60_000});
    rpc.call(method::SCHEDULE_CREATE, once.clone())
        .await
        .unwrap();
    let twice = rpc.call(method::SCHEDULE_CREATE, once).await;
    assert_eq!(code_of(twice.unwrap_err()), code::SCHEDULE_EXISTS);
    let unknown = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": "agent-99"}))
        .await;
    assert_eq!(code_of(unknown.unwrap_err()), code::AGENT_NOT_FOUND);
    db.drop().await;
}

#[tokio::test]
async fn a_tenant_schedules_only_what_its_allowlists_give() {
    let Some(db) = TestDb::empty("sched_tenant").await else {
        return;
    };
    // The default allowlists give a tenant no schedules and no background
    // runs.
    let rpc = Rpc::remote(tenant("acme", allow(&db)));
    let agent = rpc.open(on_postgres(&db, "acme")).await;
    let hourly = json!({"agent": agent, "prompt": "p", "cron": "0 * * * *"});
    let refused = rpc.call(method::SCHEDULE_CREATE, hourly.clone()).await;
    assert_eq!(
        refusal(refused.unwrap_err()),
        (code::NOT_ALLOWED, Some("schedules".into()))
    );
    let enqueue = json!({"agent": agent, "session_id": "s", "prompt": "p"});
    let refused = rpc.call(method::JOB_ENQUEUE, enqueue.clone()).await;
    assert_eq!(
        refusal(refused.unwrap_err()),
        (code::NOT_ALLOWED, Some("background_runs".into()))
    );

    let allowed = allow(&db).max_schedules(Some(2)).max_queued_jobs(Some(1));
    let rpc = Rpc::remote(tenant("acme", allowed));
    let agent = rpc.open(on_postgres(&db, "acme")).await;
    let support = rpc.open(on_postgres(&db, "acme/support")).await;
    // The default floor is a minute between fires, for intervals and for
    // the closest fires of a cron pattern.
    for fast in [
        json!({"agent": agent, "prompt": "p", "interval_ms": 59_999}),
        json!({"agent": agent, "prompt": "p", "cron": "* * * * * *"}),
        json!({"agent": agent, "prompt": "p", "cron": "0,30 * * * * *"}),
    ] {
        let refused = rpc.call(method::SCHEDULE_CREATE, fast.clone()).await;
        let error = refused.unwrap_err();
        assert_eq!(
            refusal(error),
            (code::NOT_ALLOWED, Some("schedule_interval".into())),
            "{fast}"
        );
    }
    let hourly = json!({"agent": agent, "prompt": "p", "cron": "0 * * * *"});
    rpc.call(method::SCHEDULE_CREATE, hourly.clone())
        .await
        .unwrap();
    // The cap counts the tenant's namespaces together.
    let other = json!({"agent": support, "prompt": "p", "interval_ms": 60_000});
    rpc.call(method::SCHEDULE_CREATE, other).await.unwrap();
    let refused = rpc.call(method::SCHEDULE_CREATE, hourly).await;
    assert_eq!(
        refusal(refused.unwrap_err()),
        (code::LIMIT_REACHED, Some("schedules".into()))
    );
    let enqueue = json!({"agent": agent, "session_id": "s", "prompt": "p"});
    rpc.call(method::JOB_ENQUEUE, enqueue.clone())
        .await
        .unwrap();
    let refused = rpc.call(method::JOB_ENQUEUE, enqueue).await;
    assert_eq!(
        refusal(refused.unwrap_err()),
        (code::LIMIT_REACHED, Some("jobs".into()))
    );
    db.drop().await;
}

/// Poll `job.get` until the job is in `state`.
async fn wait_for(rpc: &Rpc, agent: &str, job_id: i64, state: &str) -> JobInfo {
    for _ in 0..400 {
        let got = rpc
            .call(method::JOB_GET, json!({"agent": agent, "job_id": job_id}))
            .await
            .unwrap();
        let got = job(got);
        if got.state == state {
            return got;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("job {job_id} never reached {state}");
}

fn worker_config(owner: &str) -> WorkerConfig {
    let mut config = WorkerConfig::new(owner);
    config.poll_interval = Duration::from_millis(50);
    config.retry_delay = Duration::from_millis(100);
    config
}

#[tokio::test]
async fn a_worker_runs_a_tenants_background_runs_and_fires() {
    let Some(db) = TestDb::empty("sched_worker").await else {
        return;
    };
    let allowed = allow(&db)
        .max_schedules(Some(4))
        .max_queued_jobs(Some(4))
        .min_schedule_interval(Duration::from_millis(100));
    let acme = Arc::new(Tenant::new("acme", allowed).unwrap());
    let rpc = Rpc::remote(Access::Tenant(Arc::clone(&acme).connect().unwrap()));
    let agent = rpc.open(on_postgres(&db, "acme")).await;
    let queued = rpc
        .call(
            method::JOB_ENQUEUE,
            json!({"agent": agent, "session_id": "s1", "prompt": "go"}),
        )
        .await
        .unwrap();
    let queued = job(queued);

    let pool = linch::postgres::connect(
        &db.dsn,
        linch::postgres::CONNECT_TIMEOUT,
        4.try_into().unwrap(),
    )
    .await
    .unwrap();
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::clone(&acme));
    let roots = factory.roots();
    assert_eq!(roots, vec!["acme".to_owned()]);
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(pool.clone(), worker_config("w1")).definitions(roots, Arc::new(factory));
    let running = tokio::spawn(worker.run(shutdown.clone()));

    let done = wait_for(&rpc, &agent, queued.job_id, "done").await;
    let run_id = done.run_id.expect("the job names its run");
    let events = rpc
        .call(
            method::RUN_EVENTS,
            json!({"agent": agent, "run_id": run_id}),
        )
        .await
        .unwrap();
    let events: Events = serde_json::from_value(events).unwrap();
    match &events.events.last().unwrap().event {
        Event::Result(result) => assert_eq!(result.final_text.as_deref(), Some("checked in")),
        other => panic!("expected a result, got {other:?}"),
    }

    // A heartbeat on one session: each fire a new run there.
    let beat = rpc
        .call(
            method::SCHEDULE_CREATE,
            json!({"agent": agent, "id": "beat", "prompt": "Check in.",
                   "interval_ms": 200, "session_id": "ops"}),
        )
        .await
        .unwrap();
    assert_eq!(info(beat).fires, 0);
    let mut fired = Vec::new();
    for _ in 0..400 {
        let listed = rpc
            .call(
                method::JOB_LIST,
                json!({"agent": agent, "schedule_id": "beat"}),
            )
            .await
            .unwrap();
        fired = serde_json::from_value::<Vec<JobInfo>>(listed["jobs"].clone()).unwrap();
        if fired.iter().filter(|job| job.state == "done").count() >= 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let done: Vec<&JobInfo> = fired.iter().filter(|job| job.state == "done").collect();
    assert!(done.len() >= 2, "{fired:?}");
    assert!(
        done.iter()
            .all(|job| job.session_id.as_ref().unwrap().as_str() == "ops")
    );
    let runs: std::collections::HashSet<_> = done.iter().map(|job| job.run_id.clone()).collect();
    assert_eq!(runs.len(), done.len(), "a new run per fire");
    rpc.call(
        method::SCHEDULE_DELETE,
        json!({"agent": agent, "id": "beat"}),
    )
    .await
    .unwrap();
    shutdown.cancel();
    running.await.unwrap().unwrap();
    db.drop().await;
}

#[tokio::test]
async fn a_definition_the_tenant_no_longer_allows_fails_its_job() {
    let Some(db) = TestDb::empty("sched_readmit").await else {
        return;
    };
    let allowed = allow(&db).max_queued_jobs(Some(4));
    let acme = Arc::new(Tenant::new("acme", allowed).unwrap());
    let rpc = Rpc::remote(Access::Tenant(Arc::clone(&acme).connect().unwrap()));
    let agent = rpc.open(on_postgres(&db, "acme")).await;
    let queued = rpc
        .call(
            method::JOB_ENQUEUE,
            json!({"agent": agent, "session_id": "s1", "prompt": "go"}),
        )
        .await
        .unwrap();
    let queued = job(queued);
    // The worker's server no longer allows the scripted provider for acme.
    let stricter = Arc::new(Tenant::new("acme", Allowlists::new()).unwrap());
    let pool = linch::postgres::connect(
        &db.dsn,
        linch::postgres::CONNECT_TIMEOUT,
        4.try_into().unwrap(),
    )
    .await
    .unwrap();
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(stricter);
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(pool, worker_config("w1")).definitions(factory.roots(), Arc::new(factory));
    let running = tokio::spawn(worker.run(shutdown.clone()));
    let failed = wait_for(&rpc, &agent, queued.job_id, "failed").await;
    assert!(failed.error.unwrap().contains("scripted"));
    assert!(failed.run_id.is_none(), "nothing ran");
    shutdown.cancel();
    running.await.unwrap().unwrap();
    db.drop().await;
}
