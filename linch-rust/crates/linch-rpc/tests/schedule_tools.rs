//! `schedule_tools`: the model's `CreateSchedule`, `ListSchedules` and
//! `CancelSchedule`. They are refused without a tenant that may hold
//! schedules or an agent that can run in the background; a schedule the
//! model makes runs the same agent for the same principal, under the
//! tenant's caps, and the model sees and cancels only its own agent's
//! schedules. The Postgres tests skip without `LINCH_TEST_PG_DSN`.

mod common;

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::pg::TestDb;
use common::*;
use linch::{Envelope, Event, PermissionMode, RunId, RunStore};
use linch_rpc::protocol::{AgentOpened, Events, JobInfo, Schedules, method};
use linch_rpc::{Access, Allowlists, BackgroundAgents, ClientError, Tenant, code};
use linch_worker::{Worker, WorkerConfig};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

const SCHEDULE_TOOLS: [&str; 3] = ["CreateSchedule", "ListSchedules", "CancelSchedule"];

/// A scripted config with `schedule_tools` in namespace `namespace`,
/// running `turns`, with permission mode `skip_dangerous` under `root`.
fn config(turns: Vec<linch::ScriptedTurn>, namespace: &str, root: &Path) -> Value {
    let mut config = scripted(turns, None, "");
    config["namespace"] = json!(namespace);
    config["schedule_tools"] = json!(true);
    config["permissions"] = json!({"mode": "skip_dangerous", "project_root": root});
    config
}

fn on_postgres(mut config: Value, db: &TestDb) -> Value {
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

/// Scripted provider, skip_dangerous, `root` as the one workspace root.
fn allow(root: &Path) -> Allowlists {
    let modes = [PermissionMode::Default, PermissionMode::SkipDangerous];
    Allowlists::new()
        .scripted_provider(true)
        .permission_modes(&modes)
        .workspace_roots(&[root.to_path_buf()])
        .unwrap()
}

fn allow_pg(root: &Path, db: &TestDb) -> Allowlists {
    allow(root)
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
}

/// The `reason` of a `-32040` refusal.
fn not_background(error: ClientError) -> String {
    match error {
        ClientError::Rpc(error) => {
            assert_eq!(error.code, code::NOT_BACKGROUND, "{error:?}");
            error.data.unwrap()["reason"].as_str().unwrap().to_owned()
        }
        other => panic!("expected an error object, got {other:?}"),
    }
}

/// The tool names a run lists in its `init` event.
fn init_tools(envelopes: &[Envelope]) -> Vec<String> {
    match &envelopes[0].event {
        Event::Init(init) => init.tools.clone(),
        other => panic!("first event {other:?}"),
    }
}

/// The text of each tool result, with whether it is an error, in order.
fn results(envelopes: &[Envelope]) -> Vec<(bool, String)> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => {
                let result = serde_json::to_value(&end.result).unwrap();
                let text = result["content"][0]["text"]
                    .as_str()
                    .unwrap_or("")
                    .to_owned();
                Some((result["is_error"] == json!(true), text))
            }
            _ => None,
        })
        .collect()
}

/// The error code of each tool result, in order (`None` for a success).
fn codes(envelopes: &[Envelope]) -> Vec<Option<String>> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => Some(end.error_code.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn schedule_tools_need_a_tenant_with_schedules_and_a_background_agent() {
    let dir = tempfile::tempdir().unwrap();
    let on = config(vec![text_turn("ok")], "acme", dir.path());

    // A local connection has no caps, and a stdio server runs no worker.
    let mut local = on.clone();
    local["namespace"] = json!("rpc");
    let refusal = refused(&Rpc::new(), local).await;
    assert_eq!(refusal.as_deref(), Some("schedule_tools"));

    // Deny by default: the default allowlists give no schedules.
    let rpc = Rpc::remote(tenant("acme", allow(dir.path())));
    assert_eq!(
        refused(&rpc, on.clone()).await.as_deref(),
        Some("schedule_tools")
    );
    let mut off = on.clone();
    off["schedule_tools"] = json!(false);
    rpc.open(off).await;

    // With schedules: still only an agent that can run in the background.
    let rpc = Rpc::remote(tenant("acme", allow(dir.path()).max_schedules(Some(1))));
    let memory = rpc.call(method::AGENT_OPEN, on.clone()).await.unwrap_err();
    assert_eq!(not_background(memory), "memory_store");
    let mut hosted = on.clone();
    hosted["tools"] = json!([host_tool("lookup", "read")]);
    let refused = rpc.call(method::AGENT_OPEN, hosted).await.unwrap_err();
    assert_eq!(not_background(refused), "host_tools");
}

/// A run on `agent` in `session` for `principal`, to its end.
async fn run(rpc: &mut Rpc, agent: &str, session: &str, principal: Value) -> Vec<Envelope> {
    let params = json!({"agent": agent, "session_id": session, "prompt": "Go.",
                        "principal": principal});
    let run_id = rpc.accepted(method::RUN_START, params).await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    ended.envelopes
}

fn worker_config(owner: &str) -> WorkerConfig {
    let mut config = WorkerConfig::new(owner);
    config.poll_interval = Duration::from_millis(50);
    config.retry_delay = Duration::from_millis(100);
    config
}

async fn pool(db: &TestDb) -> linch::postgres::PgPool {
    let timeout = linch::postgres::CONNECT_TIMEOUT;
    let pool = linch::postgres::connect(&db.dsn, timeout, 4.try_into().unwrap()).await;
    pool.unwrap()
}

/// How long a test waits for a worker: generous, since a loaded machine can
/// starve it (and the worker's open can wait out other tests' migrations).
const DEADLINE: Duration = Duration::from_secs(120);

/// The first `done` fire of schedule `id`, within [`DEADLINE`].
async fn first_fire(rpc: &Rpc, agent: &str, id: &str) -> JobInfo {
    let deadline = Instant::now() + DEADLINE;
    loop {
        let listed = rpc
            .call(method::JOB_LIST, json!({"agent": agent, "schedule_id": id}))
            .await
            .unwrap();
        let jobs: Vec<JobInfo> = serde_json::from_value(listed["jobs"].clone()).unwrap();
        if let Some(done) = jobs.iter().rev().find(|job| job.state == "done") {
            return done.clone();
        }
        assert!(
            Instant::now() < deadline,
            "schedule {id} never fired: {jobs:?}"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// The model schedules a heartbeat in its session; a worker fires it, and
/// the fire runs the same definition (the same contract) in the same
/// session for the same principal. Its own `CreateSchedule` meets the
/// tenant's cap, not a permission rule the principal's role lifts.
#[tokio::test]
async fn a_model_created_schedule_fires_as_the_same_agent_for_the_same_principal() {
    let Some(db) = TestDb::empty("stools_fire").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db)
        .max_schedules(Some(1))
        .max_queued_jobs(Some(4))
        .min_schedule_interval(Duration::from_millis(100));
    let acme = Arc::new(Tenant::new("acme", allowed).unwrap());
    let mut rpc = Rpc::remote(Access::Tenant(Arc::clone(&acme).connect().unwrap()));
    let turns = vec![
        tool_turn(
            "c1",
            "CreateSchedule",
            json!({"payload": "Check in.", "interval_s": 0.2}),
        ),
        text_turn("scheduled"),
    ];
    let mut on = on_postgres(config(turns, "acme", dir.path()), &db);
    // Only a principal holding `scheduler` may schedule.
    let deny = json!({"type": "tool", "tool": "CreateSchedule", "decision": "deny",
                      "unless_role": "scheduler"});
    on["permissions"]["layers"] = json!([{"name": "tenant", "rules": [deny]}]);
    let opened: AgentOpened =
        serde_json::from_value(rpc.call(method::AGENT_OPEN, on).await.unwrap()).unwrap();
    let agent = opened.agent.clone();
    let ada = json!({"tenant": "acme", "user": "ada", "roles": ["scheduler"]});
    let envelopes = run(&mut rpc, &agent, "chat", ada.clone()).await;
    let tools = init_tools(&envelopes);
    assert!(
        SCHEDULE_TOOLS
            .iter()
            .all(|name| tools.iter().any(|t| t == name))
    );
    let said = results(&envelopes);
    assert!(
        !said[0].0 && said[0].1.starts_with("Scheduled "),
        "{said:?}"
    );

    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    let listed: Schedules = serde_json::from_value(listed).unwrap();
    let [schedule] = listed.schedules.as_slice() else {
        panic!("{listed:?}");
    };
    assert_eq!(schedule.session_id.as_ref().unwrap().as_str(), "chat");
    assert_eq!(
        (schedule.queue.as_str(), schedule.interval_ms),
        ("default", Some(200))
    );
    let pool = pool(&db).await;
    // The stored definition is the opened config, with the switch on.
    let stored: Vec<String> =
        sqlx::query_scalar("SELECT config FROM linch_worker_agents WHERE namespace = 'acme'")
            .fetch_all(&pool)
            .await
            .unwrap();
    let [stored] = stored.as_slice() else {
        panic!("one definition: {stored:?}");
    };
    let stored: Value = serde_json::from_str(stored).unwrap();
    assert_eq!(stored["schedule_tools"], json!(true));
    assert_eq!(stored["stores"]["dsn"], json!(""));
    let principal: Option<String> =
        sqlx::query_scalar("SELECT principal FROM linch_worker_schedules")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&principal.unwrap()).unwrap(),
        ada
    );

    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::clone(&acme));
    let shutdown = CancellationToken::new();
    let worker = Worker::new(pool.clone(), worker_config("w1"))
        .definitions(factory.roots(), Arc::new(factory));
    let running = tokio::spawn(worker.run(shutdown.clone()));
    let fire = first_fire(&rpc, &agent, &schedule.id).await;
    assert_eq!(fire.session_id.as_ref().unwrap().as_str(), "chat");
    let run_id: RunId = fire.run_id.expect("the fire names its run");
    rpc.call(
        method::SCHEDULE_DELETE,
        json!({"agent": agent, "id": schedule.id}),
    )
    .await
    .unwrap();
    shutdown.cancel();
    running.await.unwrap().unwrap();

    // The same contract: the worker opened the stored definition with the
    // schedule tools where `agent.open` put them.
    let record = linch::postgres::PgRunStore::new(pool.clone())
        .load("acme", &run_id)
        .await
        .unwrap();
    assert_eq!(record.contract.to_string(), opened.contract);
    let events = rpc
        .call(
            method::RUN_EVENTS,
            json!({"agent": agent, "run_id": run_id}),
        )
        .await
        .unwrap();
    let events: Events = serde_json::from_value(events).unwrap();
    let said = results(&events.events);
    // The fire acted for `ada` (the deny rule did not match) and met the
    // tenant's cap of one schedule.
    assert_eq!(codes(&events.events), [Some("limit_reached".to_owned())]);
    assert!(
        said[0].1.contains("already holds its 1 schedules"),
        "{said:?}"
    );
    match &events.events.last().unwrap().event {
        Event::Result(result) => assert_eq!(result.final_text.as_deref(), Some("scheduled")),
        other => panic!("expected a result, got {other:?}"),
    }
    db.drop().await;
}

/// A run without the role is denied by the rule: the fire's success above
/// depended on the principal it carried.
#[tokio::test]
async fn without_the_principal_the_rule_denies_the_call() {
    let Some(db) = TestDb::empty("stools_deny").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(1));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let turns = vec![
        tool_turn(
            "c1",
            "CreateSchedule",
            json!({"payload": "p", "interval_s": 3600}),
        ),
        text_turn("done"),
    ];
    let mut on = on_postgres(config(turns, "acme", dir.path()), &db);
    let deny = json!({"type": "tool", "tool": "CreateSchedule", "decision": "deny",
                      "unless_role": "scheduler"});
    on["permissions"]["layers"] = json!([{"name": "tenant", "rules": [deny]}]);
    let agent = rpc.open(on).await;
    let eve = json!({"tenant": "acme", "user": "eve", "roles": ["intern"]});
    let said = results(&run(&mut rpc, &agent, "s", eve).await);
    assert!(said[0].0 && !said[0].1.contains("Scheduled"), "{said:?}");
    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    assert_eq!(listed, json!({"schedules": []}));
    db.drop().await;
}

/// The tool takes the same admission as `schedule.create`: the interval
/// floor, and one cap that counts the client's and the model's schedules.
#[tokio::test]
async fn the_tenant_caps_hold_through_the_tool() {
    let Some(db) = TestDb::empty("stools_caps").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(2));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let create = |id: &str, input: Value| tool_turn(id, "CreateSchedule", input);
    let turns = vec![
        // Under the default floor of a minute.
        create("c1", json!({"payload": "p", "interval_s": 1})),
        create("c2", json!({"payload": "p", "cron": "* * * * * *"})),
        create(
            "c3",
            json!({"payload": "p", "cron": "0 * * * *", "timezone": "Mars/Base"}),
        ),
        create("c4", json!({"payload": "hourly", "cron": "0 * * * *"})),
        create("c5", json!({"payload": "p", "interval_s": 3600})),
        tool_turn("l1", "ListSchedules", json!({})),
        text_turn("done"),
    ];
    let agent = rpc
        .open(on_postgres(config(turns, "acme", dir.path()), &db))
        .await;
    // The client takes one place of the two.
    rpc.call(
        method::SCHEDULE_CREATE,
        json!({"agent": agent, "id": "client", "prompt": "p", "interval_ms": 3_600_000}),
    )
    .await
    .unwrap();
    let ada = json!({"tenant": "acme", "user": "ada", "roles": []});
    let envelopes = run(&mut rpc, &agent, "s", ada).await;
    let code = |name: &str| Some(name.to_owned());
    assert_eq!(
        codes(&envelopes),
        [
            code("not_allowed"),
            code("not_allowed"),
            code("invalid_schedule"),
            None,
            code("limit_reached"),
            None
        ]
    );
    let said = results(&envelopes);
    assert!(
        said[0].0 && said[0].1.contains("min_schedule_interval_ms"),
        "{said:?}"
    );
    assert!(
        said[1].0 && said[1].1.contains("min_schedule_interval_ms"),
        "{said:?}"
    );
    assert!(said[2].0, "an unknown time zone: {said:?}");
    assert!(
        !said[3].0 && said[3].1.starts_with("Scheduled "),
        "{said:?}"
    );
    assert!(
        said[4].0 && said[4].1.contains("already holds its 2 schedules"),
        "{said:?}"
    );
    // The client's schedule is the same agent's, so the model sees it.
    let listing = &said[5].1;
    assert!(listing.contains("client: p enabled=true"), "{listing}");
    assert!(listing.contains(": hourly enabled=true"), "{listing}");
    // And the client meets the cap the model filled.
    let refused = rpc
        .call(
            method::SCHEDULE_CREATE,
            json!({"agent": agent, "prompt": "p", "interval_ms": 3_600_000}),
        )
        .await
        .unwrap_err();
    assert_eq!(
        refusal(refused),
        (code::LIMIT_REACHED, Some("schedules".into()))
    );
    db.drop().await;
}

/// The model lists and cancels only its own agent's schedules: not those
/// of another config in its namespace, nor of another namespace.
#[tokio::test]
async fn the_model_reaches_only_its_own_agents_schedules() {
    let Some(db) = TestDb::empty("stools_scope").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(10));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let turns = vec![
        tool_turn("l1", "ListSchedules", json!({})),
        tool_turn("x1", "CancelSchedule", json!({"id": "theirs"})),
        tool_turn("x2", "CancelSchedule", json!({"id": "support"})),
        tool_turn("x3", "CancelSchedule", json!({"id": "mine"})),
        text_turn("done"),
    ];
    let on = on_postgres(config(turns, "acme", dir.path()), &db);
    let agent = rpc.open(on.clone()).await;
    let mut plain = on.clone();
    plain["schedule_tools"] = json!(false);
    let other = rpc.open(plain).await;
    let mut support = on;
    support["namespace"] = json!("acme/support");
    let support = rpc.open(support).await;
    let hourly = |agent: &str, id: &str| json!({"agent": agent, "id": id, "prompt": id, "cron": "0 * * * *"});
    for (handle, id) in [(&agent, "mine"), (&other, "theirs"), (&support, "support")] {
        rpc.call(method::SCHEDULE_CREATE, hourly(handle, id))
            .await
            .unwrap();
    }
    let ada = json!({"tenant": "acme", "user": "ada", "roles": []});
    let said = results(&run(&mut rpc, &agent, "s", ada).await);
    assert!(said[0].1.starts_with("mine: mine enabled=true"), "{said:?}");
    assert!(!said[0].1.contains("theirs") && !said[0].1.contains("support"));
    assert_eq!(said[1], (true, "No schedule theirs".to_owned()));
    assert_eq!(said[2], (true, "No schedule support".to_owned()));
    assert_eq!(said[3], (false, "Cancelled mine".to_owned()));
    for (handle, id) in [(&other, "theirs"), (&support, "support")] {
        let listed = rpc
            .call(method::SCHEDULE_LIST, json!({"agent": handle}))
            .await
            .unwrap();
        assert_eq!(listed["schedules"][0]["id"], json!(id));
    }
    db.drop().await;
}

/// A subagent's call is refused: the child may hold fewer tools than the
/// agent its schedule would run.
#[tokio::test]
async fn a_subagent_cannot_create_schedules() {
    let Some(db) = TestDb::empty("stools_child").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(4));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let turns = vec![
        tool_turn(
            "sub",
            "Subagent",
            json!({"description": "schedule", "prompt": "Schedule it."}),
        ),
        tool_turn(
            "c1",
            "CreateSchedule",
            json!({"payload": "p", "interval_s": 3600}),
        ),
        text_turn("child done"),
        text_turn("done"),
    ];
    let mut on = on_postgres(config(turns, "acme", dir.path()), &db);
    on["subagents"] = json!(true);
    on["config_dir"] = json!(dir.path());
    let agent = rpc.open(on).await;
    let ada = json!({"tenant": "acme", "user": "ada", "roles": []});
    let envelopes = run(&mut rpc, &agent, "s", ada).await;
    let log = serde_json::to_string(&envelopes).unwrap();
    assert!(log.contains("a subagent cannot create schedules"), "{log}");
    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    assert_eq!(listed, json!({"schedules": []}));
    db.drop().await;
}

/// The same call again (a retry keeps its `tool_use_id`) finds the
/// schedule it made instead of making a second one.
#[tokio::test]
async fn the_same_call_again_makes_one_schedule() {
    let Some(db) = TestDb::empty("stools_retry").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(4));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let input = json!({"payload": "p", "interval_s": 3600});
    let turns = vec![
        tool_turn("c1", "CreateSchedule", input.clone()),
        tool_turn("c1", "CreateSchedule", input),
        text_turn("done"),
    ];
    let agent = rpc
        .open(on_postgres(config(turns, "acme", dir.path()), &db))
        .await;
    let ada = json!({"tenant": "acme", "user": "ada", "roles": []});
    let envelopes = run(&mut rpc, &agent, "s", ada).await;
    let said = results(&envelopes);
    // Both calls ran and answered the same schedule.
    assert_eq!(codes(&envelopes), [None, None], "{said:?}");
    assert_eq!(said[0], said[1]);
    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    let listed: Schedules = serde_json::from_value(listed).unwrap();
    assert_eq!(listed.schedules.len(), 1, "{said:?}");
    assert!(listed.schedules[0].id.ends_with(":c1"), "{listed:?}");
    db.drop().await;
}

/// In mode `default` the call asks; the approved call stores the
/// principal the continuation acts for, as every call of it does.
#[tokio::test]
async fn an_approved_call_stores_the_principal_it_runs_for() {
    let Some(db) = TestDb::empty("stools_approve").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db).max_schedules(Some(4));
    let mut rpc = Rpc::remote(tenant("acme", allowed));
    let turns = vec![
        tool_turn(
            "c1",
            "CreateSchedule",
            json!({"payload": "p", "interval_s": 3600}),
        ),
        text_turn("done"),
    ];
    let mut on = on_postgres(config(turns, "acme", dir.path()), &db);
    on["permissions"]["mode"] = json!("default");
    let agent = rpc.open(on).await;
    let ada = json!({"tenant": "acme", "user": "ada", "roles": ["ops"]});
    let params = json!({"agent": agent, "session_id": "s", "prompt": "Go.", "principal": ada});
    let run_id = rpc.accepted(method::RUN_START, params).await;
    let ended = rpc.until_end(&run_id).await;
    assert!(
        matches!(ended.last(), Event::Suspended(_)),
        "{:?}",
        ended.last()
    );
    let listed = rpc
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    assert_eq!(
        listed,
        json!({"schedules": []}),
        "nothing before the approval"
    );
    let approve = json!({"agent": agent, "run_id": run_id, "principal": ada,
                         "decisions": {"c1": {"decision": "allow"}}});
    rpc.accepted(method::RUN_APPROVE, approve).await;
    let ended = rpc.until_end(&run_id).await;
    assert_eq!(codes(&ended.envelopes), [None]);
    let pool = pool(&db).await;
    let principal: Option<String> =
        sqlx::query_scalar("SELECT principal FROM linch_worker_schedules")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&principal.unwrap()).unwrap(),
        ada
    );
    db.drop().await;
}

/// A worker checks the definition again at every fire: a tenant whose
/// allowlists lost `max_schedules` fails the fire of an agent with the
/// schedule tools.
#[tokio::test]
async fn a_tenant_that_lost_its_schedules_fails_the_fire() {
    let Some(db) = TestDb::empty("stools_readmit").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let allowed = allow_pg(dir.path(), &db)
        .max_schedules(Some(1))
        .min_schedule_interval(Duration::from_millis(100));
    let rpc = Rpc::remote(tenant("acme", allowed));
    let on = on_postgres(config(vec![text_turn("ok")], "acme", dir.path()), &db);
    let agent = rpc.open(on).await;
    let beat = json!({"agent": agent, "id": "beat", "prompt": "p", "interval_ms": 100,
                      "session_id": "ops"});
    rpc.call(method::SCHEDULE_CREATE, beat).await.unwrap();

    let stricter = Arc::new(Tenant::new("acme", allow_pg(dir.path(), &db)).unwrap());
    let pool = pool(&db).await;
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(stricter);
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(pool, worker_config("w1")).definitions(factory.roots(), Arc::new(factory));
    let running = tokio::spawn(worker.run(shutdown.clone()));
    let deadline = Instant::now() + DEADLINE;
    let failed = loop {
        let listed = rpc
            .call(
                method::JOB_LIST,
                json!({"agent": agent, "schedule_id": "beat"}),
            )
            .await
            .unwrap();
        let jobs: Vec<JobInfo> = serde_json::from_value(listed["jobs"].clone()).unwrap();
        if let Some(failed) = jobs.into_iter().find(|job| job.state == "failed") {
            break failed;
        }
        assert!(Instant::now() < deadline, "no fire failed: {listed}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    };
    rpc.call(
        method::SCHEDULE_DELETE,
        json!({"agent": agent, "id": "beat"}),
    )
    .await
    .unwrap();
    shutdown.cancel();
    running.await.unwrap().unwrap();
    assert!(
        failed.error.as_deref().unwrap().contains("max_schedules"),
        "{failed:?}"
    );
    assert!(failed.run_id.is_none(), "nothing ran");
    db.drop().await;
}
