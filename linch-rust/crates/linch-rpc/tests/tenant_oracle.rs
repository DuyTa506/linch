//! Two tenants on one shared Postgres schema. Tenant B asks for the real ids
//! of tenant A (a run, a session, a workflow, a job, a schedule) through
//! every method that takes one, and for ids nobody has: the two answers
//! must have the same code, message and data once the asked id is masked, so
//! B cannot tell "A has it" from "nobody has it", and nothing of A's content
//! comes back. The Postgres tests skip without `LINCH_TEST_PG_DSN`.

mod common;

use common::pg::TestDb;
use common::*;
use linch_rpc::protocol::monitor::RUN_REPORT;
use linch_rpc::protocol::{RunAccepted, method};
use linch_rpc::{Allowlists, ClientError, WorkflowCaps};
use serde_json::{Value, json};

const SECRET: &str = "ALPHA-SECRET-PROMPT";

fn on_postgres(db: &TestDb, namespace: &str) -> Value {
    let mut config = scripted(vec![text_turn("done"), text_turn("done again")], None, "");
    config["namespace"] = json!(namespace);
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

fn rpc_of(db: &TestDb, name: &str) -> Rpc {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
        .workflow(Some(WorkflowCaps::default()))
        .max_schedules(Some(4))
        .max_queued_jobs(Some(4));
    Rpc::remote(tenant(name, allow))
}

/// What a caller can see of an answer, with `asked` (the id it named) masked.
#[derive(Debug, PartialEq)]
enum Shape {
    Ok(String),
    Err(i64, String, String),
}

fn masked(text: &str, asked: &str) -> String {
    text.replace(asked, "<asked>")
}

/// Call `name` with `params` as tenant B and reduce what came back. A call
/// that starts a run is followed to the end of that run.
async fn probe(rpc: &mut Rpc, name: &str, params: Value, asked: &str) -> Shape {
    let shape = |error: linch_rpc::ErrorObject| {
        let data = error.data.map(|data| data.to_string()).unwrap_or_default();
        Shape::Err(
            error.code,
            masked(&error.message, asked),
            masked(&data, asked),
        )
    };
    match rpc.call(name, params).await {
        Ok(value) => match serde_json::from_value::<RunAccepted>(value.clone()) {
            Ok(accepted) => match rpc.until_end(&accepted.run_id).await.error {
                Some(error) => shape(error),
                None => Shape::Ok("a run ended without an error".into()),
            },
            Err(_) => Shape::Ok(masked(&value.to_string(), asked)),
        },
        Err(ClientError::Rpc(error)) => shape(error),
        Err(other) => panic!("{name}: {other:?}"),
    }
}

/// B's answer for A's id must be a refusal, not a success that happens to
/// match: a method that answered would have read or changed A's data.
fn assert_refused(name: &str, shape: &Shape) {
    assert!(
        matches!(shape, Shape::Err(..)),
        "{name}: B was not refused: {shape:?}"
    );
}

/// The params of a method that takes a run id, with what it also requires.
fn run_params(name: &str, agent: &str, id: &str) -> Value {
    let mut params = json!({"agent": agent, "run_id": id});
    match name {
        method::RUN_APPROVE => params["decisions"] = json!({}),
        method::RUN_ANSWER => params["answers"] = json!({}),
        method::RUN_STEER => params["text"] = json!("hello"),
        _ => {}
    }
    params
}

/// What tenant A made: ids that B may guess.
struct Made {
    run: String,
    session: String,
    workflow: String,
    job: i64,
    schedule: String,
}

async fn tenant_a_makes_its_things(rpc: &mut Rpc, db: &TestDb) -> Made {
    let agent = rpc.open(on_postgres(db, "acme")).await;
    let run = rpc.start(&agent, "s-alpha", SECRET).await;
    assert!(rpc.until_end(&run).await.error.is_none());
    let started = json!({"agent": agent, "run_id": "wf-alpha"});
    rpc.call(method::WORKFLOW_START, started).await.unwrap();
    let finished = json!({"run_id": "wf-alpha", "ok": true});
    rpc.call(method::WORKFLOW_FINISH, finished).await.unwrap();
    let queued = json!({"agent": agent, "session_id": "s-job", "prompt": SECRET});
    let job = rpc.call(method::JOB_ENQUEUE, queued).await.unwrap();
    let schedule = json!({"agent": agent, "id": "digest", "prompt": SECRET,
                          "interval_ms": 3_600_000});
    rpc.call(method::SCHEDULE_CREATE, schedule).await.unwrap();
    Made {
        run: run.to_string(),
        session: "s-alpha".into(),
        workflow: "wf-alpha".into(),
        job: job["job_id"].as_i64().expect("a numeric job id"),
        schedule: "digest".into(),
    }
}

#[tokio::test]
async fn a_tenant_cannot_tell_another_tenants_id_from_one_nobody_has() {
    let Some(db) = TestDb::empty("tenant_oracle").await else {
        return;
    };
    let mut acme = rpc_of(&db, "acme");
    let made = tenant_a_makes_its_things(&mut acme, &db).await;

    // The controls: A reaches its own, so a refusal below is B's, not a typo.
    let agent = acme.open(on_postgres(&db, "acme")).await;
    let own = json!({"agent": agent, "run_id": made.run});
    let events = acme.call(method::RUN_EVENTS, own).await.unwrap();
    assert!(!events["events"].as_array().unwrap().is_empty());
    let own = json!({"agent": agent, "job_id": made.job});
    acme.call(method::JOB_GET, own).await.unwrap();
    let own = json!({"agent": agent, "run_id": made.workflow});
    acme.call(method::WORKFLOW_STATUS, own).await.unwrap();

    let mut beta = rpc_of(&db, "beta");
    let agent = beta.open(on_postgres(&db, "beta")).await;
    let missing_run = "r-nobody-has-this-0001";
    let missing_session = "s-nobody-has-this";
    let missing_workflow = "wf-nobody-has-this";
    let missing_job = 987_654_321_i64;
    let missing_schedule = "nobody-has-this";

    let run_calls = [
        method::RUN_EVENTS,
        RUN_REPORT,
        method::RUN_RESUME,
        method::RUN_APPROVE,
        method::RUN_ANSWER,
        method::RUN_STEER,
        method::RUN_CANCEL,
    ];
    for name in run_calls {
        let real = probe(
            &mut beta,
            name,
            run_params(name, &agent, &made.run),
            &made.run,
        )
        .await;
        assert_refused(name, &real);
        let params = run_params(name, &agent, missing_run);
        let gone = probe(&mut beta, name, params, missing_run).await;
        assert_eq!(real, gone, "{name}: A's run and a missing one read alike");
    }
    // Workflows: the status of A's, and B starting an id A holds.
    for name in [method::WORKFLOW_STATUS, method::WORKFLOW_START] {
        let build = |id: &str| json!({"agent": agent, "run_id": id});
        let real = probe(&mut beta, name, build(&made.workflow), &made.workflow).await;
        let gone = probe(&mut beta, name, build(missing_workflow), missing_workflow).await;
        if name == method::WORKFLOW_STATUS {
            assert_refused(name, &real);
        }
        assert_eq!(
            real, gone,
            "{name}: A's workflow and a missing one read alike"
        );
    }
    // A job, by id.
    let real = probe(
        &mut beta,
        method::JOB_GET,
        json!({"agent": agent, "job_id": made.job}),
        &made.job.to_string(),
    )
    .await;
    let gone = probe(
        &mut beta,
        method::JOB_GET,
        json!({"agent": agent, "job_id": missing_job}),
        &missing_job.to_string(),
    )
    .await;
    assert_refused(method::JOB_GET, &real);
    assert_eq!(real, gone, "job.get: A's job and a missing one read alike");
    // A schedule, by id: pause, resume and delete.
    for name in [
        method::SCHEDULE_PAUSE,
        method::SCHEDULE_RESUME,
        method::SCHEDULE_DELETE,
    ] {
        let real = json!({"agent": agent, "id": made.schedule});
        let real = probe(&mut beta, name, real, &made.schedule).await;
        let gone = json!({"agent": agent, "id": missing_schedule});
        let gone = probe(&mut beta, name, gone, missing_schedule).await;
        if name != method::SCHEDULE_DELETE {
            assert_refused(name, &real);
        }
        assert_eq!(
            real, gone,
            "{name}: A's schedule and a missing one read alike"
        );
    }
    // A session id: closing it, and starting a run on it, show B nothing of
    // A's conversation and read like a session nobody has.
    let close = |id: &str| json!({"agent": agent, "session_id": id});
    let real = probe(
        &mut beta,
        method::SESSION_CLOSE,
        close(&made.session),
        &made.session,
    )
    .await;
    let gone = probe(
        &mut beta,
        method::SESSION_CLOSE,
        close(missing_session),
        missing_session,
    )
    .await;
    assert_eq!(real, gone, "session.close");
    let run = beta.start(&agent, &made.session, "hello from beta").await;
    let ended = beta.until_end(&run).await;
    assert!(ended.error.is_none());
    let users = ended.kinds().iter().filter(|kind| *kind == "user").count();
    assert_eq!(
        users, 1,
        "B's run on A's session id starts an empty session"
    );

    // What B listed and read holds nothing of A's.
    let jobs = beta
        .call(method::JOB_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    assert_eq!(jobs["jobs"], json!([]));
    let schedules = beta
        .call(method::SCHEDULE_LIST, json!({"agent": agent}))
        .await
        .unwrap();
    assert_eq!(schedules["schedules"], json!([]));
    let all = format!("{real:?} {gone:?} {:?} {jobs} {schedules}", ended.envelopes);
    assert!(!all.contains(SECRET), "A's content reached B");
    db.drop().await;
}
