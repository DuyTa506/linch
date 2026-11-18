//! The binary's `worker` section: without one the server runs no
//! scheduler and touches no queue table; with one it fires schedules and
//! runs queued jobs, each fire exactly once and each run to its end, across
//! a server killed with SIGKILL and another started in its place. Skipped
//! without `LINCH_TEST_PG_DSN`.

mod common;

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use common::TOKEN;
use common::pg::TestDb;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::{AgentOpened, JobInfo, ScheduleInfo, method};
use linch_rpc::{Client, ClientError, code};
use serde_json::{Value, json};
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;
use tokio::process::{Child, Command};
use tokio::time::{Instant, sleep};

const MODEL: &str = "scripted-model";

/// A pool on the test schema.
async fn pool(db: &TestDb) -> PgPool {
    PgPoolOptions::new()
        .max_connections(2)
        .connect(&db.dsn)
        .await
        .expect("connect to the test schema")
}

/// Write the config and start the binary on it; it is killed on drop.
async fn server(dir: &Path, name: &str, config: &Value) -> (Child, PathBuf) {
    let socket = dir.join(format!("{name}.sock"));
    let mut config = config.clone();
    config["socket"] = json!(socket);
    let file = dir.join(format!("{name}.json"));
    std::fs::write(&file, config.to_string()).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_linch-server"))
        .arg("--config")
        .arg(&file)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(120);
    while !socket.exists() {
        assert!(Instant::now() < deadline, "the socket never appeared");
        sleep(Duration::from_millis(20)).await;
    }
    (child, socket)
}

/// A client logged in as `acme` on `socket`.
async fn client(socket: &Path) -> Client {
    let stream = tokio::net::UnixStream::connect(socket).await.unwrap();
    let (reader, writer) = stream.into_split();
    let (client, _notifications) = Client::connect(reader, writer);
    let auth = json!({"token": TOKEN});
    let _: Value = client.call(method::CONNECTION_AUTH, &auth).await.unwrap();
    client
}

/// Kill a server like a crash.
async fn crash(mut child: Child) {
    child.kill().await.unwrap();
}

/// Stop a server with SIGTERM and wait for it.
async fn terminate(mut child: Child) {
    let pid = child.id().unwrap().to_string();
    let sent = Command::new("kill").args(["-TERM", &pid]).status().await;
    assert!(sent.unwrap().success());
    let status = tokio::time::timeout(Duration::from_secs(120), child.wait()).await;
    assert!(status.expect("the server stops").unwrap().success());
}

fn agent_config(db: &TestDb, turns: Vec<linch::ScriptedTurn>, extra: Value) -> Value {
    let mut config = json!({
        "namespace": "acme",
        "spec": {"model": MODEL, "max_turns": 100},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": db.dsn, "lease_ttl_ms": 1000},
    });
    for (key, value) in extra.as_object().unwrap() {
        config[key] = value.clone();
    }
    config
}

/// A config for tenant `acme`, with a worker on the test schema when
/// `worker` is set.
fn server_config(db: &TestDb, worker: bool, allow: Value) -> Value {
    let mut allowed = json!({
        "scripted_provider": true,
        "postgres_dsns": [db.dsn],
        "max_schedules": 4,
        "max_queued_jobs": 16,
        "min_schedule_interval_ms": 100,
    });
    for (key, value) in allow.as_object().unwrap() {
        allowed[key] = value.clone();
    }
    let mut config = json!({"tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allowed}}});
    if worker {
        config["worker"] = json!({
            "dsn": {"value": db.dsn},
            "poll_interval_ms": 50,
            "visibility_timeout_ms": 2000,
            "retry_delay_ms": 100,
        });
    }
    config
}

async fn open(client: &Client, config: &Value) -> String {
    let opened: AgentOpened = client.call(method::AGENT_OPEN, config).await.unwrap();
    opened.agent
}

async fn scalar(pool: &PgPool, sql: &'static str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(pool).await.unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_server_without_a_worker_runs_no_scheduler() {
    let Some(db) = TestDb::empty("no_worker").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let (child, socket) = server(dir.path(), "a", &server_config(&db, false, json!({}))).await;
    let client = client(&socket).await;
    let turns = vec![TurnBuilder::new(MODEL).text("hi").build()];
    let agent = open(&client, &agent_config(&db, turns, json!({}))).await;
    let start = json!({"agent": agent, "session_id": "s1", "prompt": "go"});
    let _: Value = client.call(method::RUN_START, &start).await.unwrap();
    let pool = pool(&db).await;
    let deadline = Instant::now() + Duration::from_secs(120);
    while scalar(
        &pool,
        "SELECT count(*) FROM linch_runs WHERE status = 'completed'",
    )
    .await
        == 0
    {
        assert!(Instant::now() < deadline, "the run never completed");
        sleep(Duration::from_millis(20)).await;
    }
    // Nothing created the queue tables: no scheduler call was made.
    let tables: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('linch_worker_migrations')::text")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(tables, None);

    // A due schedule in the schema stays due: nothing polls it.
    linch_worker::migrate(&pool).await.unwrap();
    let definition = agent_config(&db, vec![], json!({})).to_string();
    let schedule = linch_worker::Schedule {
        namespace: "acme".into(),
        id: "due".into(),
        queue: "default".into(),
        session_id: None,
        prompt: "go".into(),
        timing: linch_worker::Timing::Every(Duration::from_millis(100)),
        agent: Some(definition),
        principal: None,
    };
    linch_worker::create_schedule(&pool, &schedule, None)
        .await
        .unwrap();
    sleep(Duration::from_millis(1500)).await;
    let fires = scalar(&pool, "SELECT fires FROM linch_worker_schedules").await;
    let jobs = scalar(&pool, "SELECT count(*) FROM linch_worker_jobs").await;
    assert_eq!((fires, jobs), (0, 0));
    terminate(child).await;
    db.drop().await;
}

/// A schedule is stored in the database of the agent that made it, and a
/// worker polls only its own: one on another database never fires it, and
/// one on the agent's fires it there, so a fire never runs against a
/// database the agent did not name.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_schedule_runs_only_where_its_agent_stores() {
    let Some(theirs) = TestDb::empty("wdsn_worker").await else {
        return;
    };
    let Some(ours) = TestDb::empty("wdsn_agent").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    // A worker on `theirs`; the tenant may also store in `ours`.
    let allow = json!({"postgres_dsns": [theirs.dsn, ours.dsn]});
    let config = server_config(&theirs, true, allow.clone());
    let (child, socket) = server(dir.path(), "a", &config).await;
    let client = client(&socket).await;
    let turns = vec![TurnBuilder::new(MODEL).text("tick").build()];
    let agent = open(&client, &agent_config(&ours, turns, json!({}))).await;
    let create = json!({"agent": agent, "id": "tick", "prompt": "Tick.", "interval_ms": 100});
    let _: ScheduleInfo = client.call(method::SCHEDULE_CREATE, &create).await.unwrap();
    sleep(Duration::from_millis(1500)).await;
    let (our_pool, their_pool) = (pool(&ours).await, pool(&theirs).await);
    let schedules = "SELECT count(*) FROM linch_worker_schedules";
    assert_eq!(
        scalar(&their_pool, schedules).await,
        0,
        "stored with the agent"
    );
    let fires = "SELECT coalesce(sum(fires), 0)::bigint FROM linch_worker_schedules";
    assert_eq!(scalar(&our_pool, fires).await, 0, "the worker never saw it");
    terminate(child).await;

    // A worker on the agent's database fires it, and the run is there.
    let config = server_config(&ours, true, allow);
    let (child, _socket) = server(dir.path(), "b", &config).await;
    let completed = "SELECT count(*) FROM linch_runs WHERE status = 'completed'";
    let deadline = Instant::now() + Duration::from_secs(120);
    while scalar(&our_pool, completed).await == 0 {
        assert!(Instant::now() < deadline, "no fire ran");
        sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        scalar(&their_pool, "SELECT count(*) FROM linch_runs").await,
        0
    );
    terminate(child).await;
    ours.drop().await;
    theirs.drop().await;
}

/// Wait until every job of the schema has ended, and none failed.
async fn all_ended(pool: &PgPool) {
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let failed = scalar(
            pool,
            "SELECT count(*) FROM linch_worker_jobs WHERE failed_at IS NOT NULL",
        )
        .await;
        assert_eq!(failed, 0, "a job failed");
        if scalar(pool, "SELECT count(*) FROM linch_worker_jobs").await == 0 {
            return;
        }
        assert!(Instant::now() < deadline, "the jobs never ended");
        sleep(Duration::from_millis(50)).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_fire_runs_once_across_a_killed_server() {
    let Some(db) = TestDb::empty("fire_crash").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let config = server_config(&db, true, json!({}));
    let (first, socket) = server(dir.path(), "a", &config).await;
    let a = client(&socket).await;
    let turns = vec![TurnBuilder::new(MODEL).text("tick").build()];
    let agent = open(&a, &agent_config(&db, turns.clone(), json!({}))).await;
    let create = json!({"agent": agent, "id": "tick", "prompt": "Tick.", "interval_ms": 250});
    let created: ScheduleInfo = a.call(method::SCHEDULE_CREATE, &create).await.unwrap();
    assert_eq!(created.fires, 0);
    sleep(Duration::from_millis(1300)).await;
    // Down for a while: the fires it misses coalesce into the next one.
    crash(first).await;
    sleep(Duration::from_millis(700)).await;
    let (second, socket) = server(dir.path(), "b", &config).await;
    sleep(Duration::from_millis(1500)).await;
    let b = client(&socket).await;
    let agent = open(&b, &agent_config(&db, turns, json!({}))).await;
    let pause = json!({"agent": agent, "id": "tick"});
    let paused: ScheduleInfo = b.call(method::SCHEDULE_PAUSE, &pause).await.unwrap();
    let pool = pool(&db).await;
    all_ended(&pool).await;
    terminate(second).await;

    // Each fire once, on the schedule's grid; the ticks missed while it
    // was down (or while a fire's job was unfinished) are skips, not runs.
    let fires: Vec<i64> = sqlx::query_scalar(
        "SELECT (extract(epoch FROM fire_at) * 1000)::bigint FROM linch_worker_history \
         WHERE schedule_id = 'tick' ORDER BY fire_at",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    assert!(fires.len() >= 4, "{fires:?}");
    assert_eq!(u64::try_from(fires.len()).unwrap(), paused.fires);
    for pair in fires.windows(2) {
        let gap = pair[1] - pair[0];
        assert!(gap > 0 && gap % 250 == 0, "{fires:?}");
    }
    // The downtime alone spans at least two ticks.
    assert!(paused.skips >= 2, "{paused:?}");
    // Each fire is one run in its own session, run to its end.
    let runs = scalar(&pool, "SELECT count(*) FROM linch_runs").await;
    let completed = scalar(
        &pool,
        "SELECT count(*) FROM linch_runs WHERE status = 'completed'",
    )
    .await;
    let sessions = scalar(&pool, "SELECT count(DISTINCT session_id) FROM linch_runs").await;
    let len = i64::try_from(fires.len()).unwrap();
    assert_eq!((runs, completed, sessions), (len, len, len));
    db.drop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_run_whose_server_was_killed_finishes_on_another() {
    let Some(db) = TestDb::empty("run_crash").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(workspace.path()).unwrap();
    let allow = json!({"exec_tools": true, "workspace_roots": [root],
                       "permission_modes": ["skip_dangerous"]});
    let config = server_config(&db, true, allow);
    let (first, socket) = server(dir.path(), "a", &config).await;
    let a = client(&socket).await;
    let sleeping = TurnBuilder::new(MODEL)
        .tool_call("call_0", "Bash", &json!({"command": "sleep 2"}))
        .build();
    let turns = vec![sleeping, TurnBuilder::new(MODEL).text("slept").build()];
    let extra = json!({"workspace": root,
                       "permissions": {"mode": "skip_dangerous", "project_root": root}});
    let agent = open(&a, &agent_config(&db, turns, extra)).await;
    let enqueue = json!({"agent": agent, "session_id": "s1", "prompt": "Sleep."});
    let queued: JobInfo = a.call(method::JOB_ENQUEUE, &enqueue).await.unwrap();
    let pool = pool(&db).await;
    // Kill the server while the run waits on its tool.
    let deadline = Instant::now() + Duration::from_secs(120);
    while scalar(
        &pool,
        "SELECT count(*) FROM linch_runs WHERE status = 'running'",
    )
    .await
        == 0
    {
        assert!(Instant::now() < deadline, "the run never started");
        sleep(Duration::from_millis(20)).await;
    }
    sleep(Duration::from_millis(500)).await;
    crash(first).await;
    let run_id: String = sqlx::query_scalar("SELECT run_id FROM linch_runs")
        .fetch_one(&pool)
        .await
        .unwrap();

    let (second, _socket) = server(dir.path(), "b", &config).await;
    all_ended(&pool).await;
    terminate(second).await;
    let (status, epoch): (String, i64) =
        sqlx::query_as("SELECT status, epoch FROM linch_runs WHERE run_id = $1")
            .bind(&run_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(status, "completed");
    assert!(epoch >= 2, "another server claimed it");
    let (job_run, attempts): (String, i32) =
        sqlx::query_as("SELECT run_id, attempts FROM linch_worker_history WHERE id = $1")
            .bind(queued.job_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(job_run, run_id, "the same run, one job");
    assert!(attempts >= 2);
    assert_eq!(scalar(&pool, "SELECT count(*) FROM linch_runs").await, 1);
    db.drop().await;
}

/// With `worker.retention` a server deletes the runs and sessions that
/// ended or idled past the windows, and a client that asks for a deleted
/// run gets the code of its own (`-32050`), not a plain not-found (`-32021`).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_worker_with_retention_prunes_and_clients_see_a_pruned_run() {
    let Some(db) = TestDb::empty("retention").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let (first, socket) = server(dir.path(), "a", &server_config(&db, false, json!({}))).await;
    let a = client(&socket).await;
    let turns = vec![TurnBuilder::new(MODEL).text("hi").build()];
    let config = agent_config(&db, turns, json!({}));
    let agent = open(&a, &config).await;
    let start = json!({"agent": agent, "session_id": "s1", "prompt": "go"});
    let _: Value = a.call(method::RUN_START, &start).await.unwrap();
    let pool = pool(&db).await;
    let completed = "SELECT count(*) FROM linch_runs WHERE status = 'completed'";
    let deadline = Instant::now() + Duration::from_secs(120);
    while scalar(&pool, completed).await == 0 {
        assert!(Instant::now() < deadline, "the run never completed");
        sleep(Duration::from_millis(20)).await;
    }
    let run_id: String = sqlx::query_scalar("SELECT run_id FROM linch_runs")
        .fetch_one(&pool)
        .await
        .unwrap();
    terminate(first).await;
    // Two days on: the run ended and its session was last written then.
    for sql in [
        "UPDATE linch_runs SET ended_at = now() - interval '2 days'",
        "UPDATE linch_sessions SET updated_at = now() - interval '2 days'",
    ] {
        sqlx::query(sql).execute(&pool).await.unwrap();
    }

    let mut with_worker = server_config(&db, true, json!({}));
    with_worker["worker"]["retention"] = json!({"terminal_runs_days": 1, "idle_sessions_days": 1});
    let (second, socket) = server(dir.path(), "b", &with_worker).await;
    let deadline = Instant::now() + Duration::from_secs(120);
    let both = "SELECT (SELECT count(*) FROM linch_runs) + (SELECT count(*) FROM linch_sessions)";
    while scalar(&pool, both).await != 0 {
        assert!(Instant::now() < deadline, "the pass never deleted them");
        sleep(Duration::from_millis(50)).await;
    }
    let b = client(&socket).await;
    let agent = open(&b, &config).await;
    let by_id = json!({"agent": agent, "run_id": run_id});
    let read = b.call::<_, Value>(method::RUN_EVENTS, &by_id).await;
    assert!(
        matches!(&read, Err(ClientError::Rpc(e)) if e.code == code::RUN_PRUNED),
        "{read:?}"
    );
    // A wrong id is the plain not-found.
    let wrong = json!({"agent": agent, "run_id": "no-such-run"});
    let missing = b.call::<_, Value>(method::RUN_EVENTS, &wrong).await;
    assert!(
        matches!(&missing, Err(ClientError::Rpc(e)) if e.code == code::NOT_FOUND),
        "{missing:?}"
    );
    terminate(second).await;
    db.drop().await;
}
