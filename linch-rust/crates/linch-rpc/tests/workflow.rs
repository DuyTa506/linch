//! Directed workflows over RPC: a function in the client drives agent
//! calls, steps and interrupts that the server journals in Postgres.
//! The tests skip without `LINCH_TEST_PG_DSN`.

mod common;

use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::ScriptedTurn;
use linch::Usage;
use linch::events::kinds::WorkflowKind;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, ClientError, WorkflowCaps, code};
use serde_json::{Value, json};

fn on_postgres(db: &TestDb, turns: Vec<ScriptedTurn>) -> Value {
    let mut config = scripted(turns, None, "");
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn, "lease_ttl_ms": 1000});
    config
}

fn priced(text: &str) -> ScriptedTurn {
    let usage = Usage {
        input_tokens: 10,
        output_tokens: 5,
        ..Usage::default()
    };
    TurnBuilder::new(MODEL).text(text).usage(usage).build()
}

async fn start(rpc: &Rpc, agent: &str, id: &str) -> Result<Value, ClientError> {
    rpc.call(
        method::WORKFLOW_START,
        json!({"agent": agent, "run_id": id}),
    )
    .await
}

/// `workflow.start`, again while the connection that held the workflow
/// before is still ending.
async fn start_when_free(rpc: &Rpc, agent: &str, id: &str) -> Value {
    for _ in 0..100 {
        match start(rpc, agent, id).await {
            Err(error) if code_of_ref(&error) == Some(code::WORKFLOW_HELD) => {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            other => return other.unwrap(),
        }
    }
    panic!("the workflow was never freed");
}

fn code_of_ref(error: &ClientError) -> Option<i64> {
    match error {
        ClientError::Rpc(error) => Some(error.code),
        _ => None,
    }
}

async fn call(rpc: &Rpc, id: &str, prompt: &str, extra: Value) -> Result<Value, ClientError> {
    let mut params = json!({"run_id": id, "prompt": prompt});
    for (key, value) in extra.as_object().unwrap() {
        params[key] = value.clone();
    }
    rpc.call(method::WORKFLOW_AGENT, params).await
}

/// A journaled step: its value, from the journal or from `value`.
async fn step(rpc: &Rpc, id: &str, name: &str, key: Value, value: Value) -> (bool, Value) {
    let params = json!({"run_id": id, "name": name, "key": key});
    let begun = rpc.call(method::WORKFLOW_STEP_BEGIN, params).await.unwrap();
    if begun["replayed"] == json!(true) {
        return (true, begun["value"].clone());
    }
    let params = json!({"run_id": id, "ticket": begun["ticket"], "value": value});
    rpc.call(method::WORKFLOW_STEP_COMPLETE, params)
        .await
        .unwrap();
    (false, value)
}

async fn finish(rpc: &Rpc, id: &str, ok: bool) -> Value {
    let params = json!({"run_id": id, "ok": ok});
    rpc.call(method::WORKFLOW_FINISH, params).await.unwrap()
}

fn code_of_call(result: Result<Value, ClientError>) -> (i64, Value) {
    match result.unwrap_err() {
        ClientError::Rpc(error) => (error.code, error.data.unwrap_or(Value::Null)),
        other => panic!("expected an error object, got {other:?}"),
    }
}

#[tokio::test]
async fn a_three_stage_workflow_replays_whole_on_a_new_connection() {
    let Some(db) = TestDb::empty("wf_stages").await else {
        return;
    };
    let schema = json!({"name": "totals", "schema": {"type": "object",
        "properties": {"total": {"type": "number"}}, "required": ["total"]}});
    let mut rpc = Rpc::new();
    let turns = vec![
        text_turn("found"),
        text_turn("found"),
        text_turn(r#"{"total": 4}"#),
    ];
    let agent = rpc.open(on_postgres(&db, turns)).await;
    let started = start(&rpc, &agent, "flow").await.unwrap();
    assert_eq!(started["resumed"], json!(false));

    // Stage 1: a fan-out of two calls, each in its own branch.
    let (first, second) = tokio::join!(
        call(&rpc, "flow", "scan a", json!({"branch": "0"})),
        call(&rpc, "flow", "scan b", json!({"branch": "1"}))
    );
    let (first, second) = (first.unwrap(), second.unwrap());
    assert_eq!(first["text"], "found");
    assert_eq!(first["replayed"], json!(false));
    // Stage 2: a step whose function ran in the client.
    let (replayed, value) = step(&rpc, "flow", "merge", json!("m1"), json!({"n": 2})).await;
    assert!(!replayed);
    // Stage 3: the next prompt is built from the steps' values; the answer
    // is checked JSON.
    let prompt = format!("total {value}");
    let third = call(&rpc, "flow", &prompt, json!({"output_schema": schema}))
        .await
        .unwrap();
    assert_eq!(third["text"], r#"{"total":4}"#);
    assert_eq!(third["structured"], json!({"total": 4}));
    assert_eq!(finish(&rpc, "flow", true).await["status"], "completed");
    rpc.drain_notifications().await;
    let kinds: Vec<_> = rpc.workflow_events.iter().map(|e| e.event.kind).collect();
    assert_eq!(
        kinds
            .iter()
            .filter(|k| **k == WorkflowKind::AgentEnd)
            .count(),
        3
    );
    assert!(kinds.contains(&WorkflowKind::StepEnd));
    assert!(rpc.workflow_events.iter().all(|e| e.run_id == "flow"));
    assert!(first["session_id"] != second["session_id"]);

    // A new connection with a provider that has no turns at all: every
    // call replays from the journal, and none could run.
    let mut other = Rpc::new();
    let agent = other.open(on_postgres(&db, vec![])).await;
    let started = start_when_free(&other, &agent, "flow").await;
    assert_eq!(started["resumed"], json!(true));
    assert_eq!(started["records"], json!(4));
    assert_eq!(started["previous"], "completed");
    let (a, b) = tokio::join!(
        call(&other, "flow", "scan a", json!({"branch": "0"})),
        call(&other, "flow", "scan b", json!({"branch": "1"}))
    );
    assert_eq!(
        (
            a.unwrap()["replayed"].clone(),
            b.unwrap()["replayed"].clone()
        ),
        (json!(true), json!(true))
    );
    let (replayed, value) = step(&other, "flow", "merge", json!("m1"), json!(null)).await;
    assert!(replayed);
    assert_eq!(value, json!({"n": 2}));
    let prompt = format!("total {value}");
    let third = call(&other, "flow", &prompt, json!({"output_schema": schema}))
        .await
        .unwrap();
    assert_eq!(third["replayed"], json!(true));
    assert_eq!(third["structured"], json!({"total": 4}));
    finish(&other, "flow", true).await;
    other.drain_notifications().await;
    let kinds: Vec<_> = other.workflow_events.iter().map(|e| e.event.kind).collect();
    assert!(!kinds.contains(&WorkflowKind::AgentStart), "{kinds:?}");
    db.drop().await;
}

#[tokio::test]
async fn a_client_that_dies_mid_way_resumes_without_repeating_finished_calls() {
    let Some(db) = TestDb::empty("wf_client_crash").await else {
        return;
    };
    {
        let rpc = Rpc::new();
        let agent = rpc.open(on_postgres(&db, vec![text_turn("one")])).await;
        start(&rpc, &agent, "w").await.unwrap();
        assert_eq!(
            call(&rpc, "w", "p1", json!({})).await.unwrap()["text"],
            "one"
        );
        step(&rpc, "w", "s1", Value::Null, json!(1)).await;
        // A step begun and never completed: the client died inside it.
        let params = json!({"run_id": "w", "name": "s2"});
        rpc.call(method::WORKFLOW_STEP_BEGIN, params).await.unwrap();
    }
    // The next client has one turn: only the call after the crash can use it.
    let rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db, vec![text_turn("two")])).await;
    let started = start_when_free(&rpc, &agent, "w").await;
    assert_eq!(
        (started["resumed"].clone(), started["previous"].clone()),
        (json!(true), json!("running"))
    );
    assert_eq!(
        call(&rpc, "w", "p1", json!({})).await.unwrap()["replayed"],
        json!(true)
    );
    let (replayed, _) = step(&rpc, "w", "s1", Value::Null, json!(null)).await;
    assert!(replayed);
    let (replayed, _) = step(&rpc, "w", "s2", Value::Null, json!(2)).await;
    assert!(!replayed, "a step that never completed runs again");
    let last = call(&rpc, "w", "p3", json!({})).await.unwrap();
    assert_eq!(
        (last["text"].clone(), last["replayed"].clone()),
        (json!("two"), json!(false))
    );
    assert_eq!(finish(&rpc, "w", true).await["status"], "completed");
    db.drop().await;
}

#[tokio::test]
async fn an_edited_prompt_reruns_only_that_call() {
    let Some(db) = TestDb::empty("wf_edit").await else {
        return;
    };
    {
        let rpc = Rpc::new();
        let turns = vec![text_turn("a"), text_turn("b")];
        let agent = rpc.open(on_postgres(&db, turns)).await;
        start(&rpc, &agent, "w").await.unwrap();
        call(&rpc, "w", "first", json!({})).await.unwrap();
        call(&rpc, "w", "second", json!({})).await.unwrap();
        finish(&rpc, "w", true).await;
    }
    let rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db, vec![text_turn("c")])).await;
    start_when_free(&rpc, &agent, "w").await;
    let edited = call(&rpc, "w", "first, edited", json!({})).await.unwrap();
    assert_eq!(
        (edited["text"].clone(), edited["replayed"].clone()),
        (json!("c"), json!(false))
    );
    let kept = call(&rpc, "w", "second", json!({})).await.unwrap();
    assert_eq!(
        (kept["text"].clone(), kept["replayed"].clone()),
        (json!("b"), json!(true))
    );
    // The same prompt under another name or another key is another call.
    let named = call(&rpc, "w", "second", json!({"key": "other-key"})).await;
    assert_eq!(
        code_of_call(named).0,
        code::WORKFLOW_CALL_FAILED,
        "no turn is left"
    );
    db.drop().await;
}

#[tokio::test]
async fn identical_calls_replay_by_occurrence_and_by_branch() {
    let Some(db) = TestDb::empty("wf_identical").await else {
        return;
    };
    let turns = vec![
        text_turn("x"),
        text_turn("x"),
        text_turn("x"),
        text_turn("x"),
    ];
    {
        let rpc = Rpc::new();
        let agent = rpc.open(on_postgres(&db, turns)).await;
        start(&rpc, &agent, "w").await.unwrap();
        // Two identical calls in order, then two identical calls in branches.
        for _ in 0..2 {
            call(&rpc, "w", "same", json!({})).await.unwrap();
        }
        let (a, b) = tokio::join!(
            call(&rpc, "w", "same", json!({"branch": "0"})),
            call(&rpc, "w", "same", json!({"branch": "1"}))
        );
        a.unwrap();
        b.unwrap();
        finish(&rpc, "w", true).await;
    }
    let rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db, vec![])).await;
    let started = start_when_free(&rpc, &agent, "w").await;
    assert_eq!(started["records"], json!(4));
    for _ in 0..2 {
        assert_eq!(
            call(&rpc, "w", "same", json!({})).await.unwrap()["replayed"],
            json!(true)
        );
    }
    // The branches reach their calls in the opposite order this time.
    let (b, a) = tokio::join!(
        call(&rpc, "w", "same", json!({"branch": "1"})),
        call(&rpc, "w", "same", json!({"branch": "0"}))
    );
    assert_eq!(
        (
            a.unwrap()["replayed"].clone(),
            b.unwrap()["replayed"].clone()
        ),
        (json!(true), json!(true))
    );
    db.drop().await;
}

#[tokio::test]
async fn an_interrupt_waits_for_an_answer_across_a_restart() {
    let Some(db) = TestDb::empty("wf_interrupt").await else {
        return;
    };
    {
        let rpc = Rpc::new();
        let agent = rpc.open(on_postgres(&db, vec![text_turn("draft")])).await;
        start(&rpc, &agent, "w").await.unwrap();
        call(&rpc, "w", "plan", json!({})).await.unwrap();
        let ask = json!({"run_id": "w", "key": "approve", "payload": {"plan": "draft"}});
        let parked = rpc.call(method::WORKFLOW_INTERRUPT, ask).await.unwrap();
        assert_eq!(parked["suspended"], json!(true));
        assert_eq!(finish(&rpc, "w", false).await["status"], "awaiting_input");
        let status = json!({"agent": agent, "run_id": "w"});
        let status = rpc.call(method::WORKFLOW_STATUS, status).await.unwrap();
        assert_eq!(status["status"], "awaiting_input");
        assert_eq!(status["interrupt"]["key"], "approve");
        assert_eq!(status["interrupt"]["payload"], json!({"plan": "draft"}));
    }
    // Another server answers: the answer is journaled where it was asked.
    let rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db, vec![])).await;
    let params = json!({"agent": agent, "run_id": "w", "resume": {"approve": true}});
    let started = rpc.call(method::WORKFLOW_START, params).await.unwrap();
    assert_eq!(started["previous"], "awaiting_input");
    assert_eq!(
        call(&rpc, "w", "plan", json!({})).await.unwrap()["replayed"],
        json!(true)
    );
    let ask = json!({"run_id": "w", "key": "approve", "payload": null});
    let answer = rpc
        .call(method::WORKFLOW_INTERRUPT, ask.clone())
        .await
        .unwrap();
    assert_eq!(
        (answer["suspended"].clone(), answer["value"].clone()),
        (json!(false), json!(true))
    );
    finish(&rpc, "w", true).await;
    // Later starts need no `resume`: the answer is in the journal.
    let again = Rpc::new();
    let agent = again.open(on_postgres(&db, vec![])).await;
    start_when_free(&again, &agent, "w").await;
    let answer = again.call(method::WORKFLOW_INTERRUPT, ask).await.unwrap();
    assert_eq!(answer["value"], json!(true));
    db.drop().await;
}

#[tokio::test]
async fn two_clients_cannot_drive_one_workflow_and_the_first_keeps_writing() {
    let Some(db) = TestDb::empty("wf_held").await else {
        return;
    };
    let first = Rpc::new();
    let one = first.open(on_postgres(&db, vec![])).await;
    start(&first, &one, "w").await.unwrap();
    let second = Rpc::new();
    let two = second.open(on_postgres(&db, vec![])).await;
    assert_eq!(
        code_of_call(start(&second, &two, "w").await).0,
        code::WORKFLOW_HELD
    );
    // The same connection may not start it twice either.
    assert_eq!(
        code_of_call(start(&first, &one, "w").await).0,
        code::WORKFLOW_HELD
    );
    // The refusal claimed nothing: the first client's writes still land.
    let (replayed, _) = step(&first, "w", "s", Value::Null, json!(1)).await;
    assert!(!replayed);
    assert_eq!(finish(&first, "w", true).await["status"], "completed");
    // Once it finishes the workflow is free.
    let started = start_when_free(&second, &two, "w").await;
    assert_eq!(started["records"], json!(1));
    db.drop().await;
}

#[tokio::test]
async fn a_call_for_a_workflow_that_was_never_started_is_refused() {
    let rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![], None, "")).await;
    let refused = call(&rpc, "nope", "p", json!({})).await;
    assert_eq!(code_of_call(refused).0, code::WORKFLOW_NOT_FOUND);
    let blank = start(&rpc, &agent, " ").await;
    assert_eq!(code_of_call(blank).0, code::INVALID_PARAMS);
}

#[tokio::test]
async fn a_memory_agent_journals_within_its_connection() {
    let rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("a")], None, "")).await;
    start(&rpc, &agent, "w").await.unwrap();
    call(&rpc, "w", "p", json!({})).await.unwrap();
    finish(&rpc, "w", true).await;
    let started = start(&rpc, &agent, "w").await.unwrap();
    assert_eq!(
        (started["resumed"].clone(), started["records"].clone()),
        (json!(true), json!(1))
    );
    assert_eq!(
        call(&rpc, "w", "p", json!({})).await.unwrap()["replayed"],
        json!(true)
    );
    // An agent with workflows driven cannot be closed from under them.
    let closed = rpc.call(method::AGENT_CLOSE, json!({"agent": agent})).await;
    assert_eq!(code_of_call(closed).0, code::RUN_BUSY);
}

#[tokio::test]
async fn the_budget_stops_the_next_call_and_counts_replays() {
    let rpc = Rpc::new();
    let agent = rpc
        .open(scripted(vec![priced("a"), priced("b")], None, ""))
        .await;
    let params = json!({"agent": agent, "run_id": "w", "budget": {"max_tokens": 15}});
    rpc.call(method::WORKFLOW_START, params).await.unwrap();
    let first = call(&rpc, "w", "p1", json!({})).await.unwrap();
    assert_eq!(
        (first["tokens"].clone(), first["cost_usd"].clone()),
        (json!(15), json!(0.0))
    );
    let (kind, data) = code_of_call(call(&rpc, "w", "p2", json!({})).await);
    assert_eq!(kind, code::WORKFLOW_BUDGET);
    assert_eq!(
        (data["spent_tokens"].clone(), data["max_tokens"].clone()),
        (json!(15), json!(15))
    );
}

#[tokio::test]
async fn a_cancelled_call_aborts_its_run_and_journals_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let rpc = Rpc::new();
    let sleep = tool_turn("c1", "Bash", json!({"command": "sleep 30"}));
    let config = scripted(
        vec![sleep, text_turn("unused")],
        Some(dir.path()),
        "skip_dangerous",
    );
    let agent = rpc.open(config).await;
    start(&rpc, &agent, "w").await.unwrap();
    let blocked = call(&rpc, "w", "sleep", json!({"call_id": "slow"}));
    let cancel = async {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let params = json!({"run_id": "w", "call_id": "slow"});
        rpc.call(method::WORKFLOW_CANCEL, params).await.unwrap()
    };
    let (blocked, cancelled) = tokio::join!(blocked, cancel);
    assert_eq!(cancelled["cancelled"], json!(1));
    let (kind, data) = code_of_call(blocked);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::WORKFLOW_CALL_FAILED, json!("aborted"))
    );
    assert_eq!(finish(&rpc, "w", false).await["status"], "failed");
    let status = json!({"agent": agent, "run_id": "w"});
    let status = rpc.call(method::WORKFLOW_STATUS, status).await.unwrap();
    assert_eq!(status["records"], json!(0));
}

fn tenant_rpc(db: &TestDb, name: &str, caps: Option<WorkflowCaps>) -> Rpc {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
        .workflow(caps);
    Rpc::remote(tenant(name, allow))
}

fn tenant_config(db: &TestDb, name: &str, turns: Vec<ScriptedTurn>) -> Value {
    let mut config = on_postgres(db, turns);
    config["namespace"] = json!(name);
    config
}

#[tokio::test]
async fn workflows_are_denied_to_a_tenant_unless_its_allowlists_say_so() {
    let Some(db) = TestDb::empty("wf_denied").await else {
        return;
    };
    let rpc = tenant_rpc(&db, "acme", None);
    let agent = rpc.open(tenant_config(&db, "acme", vec![])).await;
    let refused = start(&rpc, &agent, "w").await;
    let (kind, data) = code_of_call(refused);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::NOT_ALLOWED, json!("workflow"))
    );
    db.drop().await;
}

#[tokio::test]
async fn a_tenants_caps_bound_its_workflows_records_and_budget() {
    let Some(db) = TestDb::empty("wf_caps").await else {
        return;
    };
    let caps = WorkflowCaps {
        max_workflows: 1,
        max_records: 2,
        max_fan_out: 1,
        max_tokens: Some(100),
        ..WorkflowCaps::default()
    };
    let rpc = tenant_rpc(&db, "acme", Some(caps));
    let turns = vec![priced("a"), priced("b"), priced("c")];
    let agent = rpc.open(tenant_config(&db, "acme", turns)).await;
    // A budget above the ceiling is refused; none gets the ceiling.
    let high = json!({"agent": agent, "run_id": "w", "budget": {"max_tokens": 101}});
    let (kind, data) = code_of_call(rpc.call(method::WORKFLOW_START, high).await);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::NOT_ALLOWED, json!("workflow_budget"))
    );
    let started = start(&rpc, &agent, "w").await.unwrap();
    assert_eq!(
        started["limits"],
        json!({"max_fan_out": 1, "max_records": 2})
    );
    // One workflow at once.
    let (kind, data) = code_of_call(start(&rpc, &agent, "other").await);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::LIMIT_REACHED, json!("workflows"))
    );
    // Two records, then the third call is refused before it spends.
    call(&rpc, "w", "p1", json!({})).await.unwrap();
    step(&rpc, "w", "s", Value::Null, json!(1)).await;
    let (kind, data) = code_of_call(call(&rpc, "w", "p3", json!({})).await);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::LIMIT_REACHED, json!("journal_records"))
    );
    db.drop().await;
}

#[tokio::test]
async fn a_tenant_cannot_have_more_calls_in_flight_than_its_fan_out() {
    let dir = tempfile::tempdir().unwrap();
    let caps = WorkflowCaps {
        max_fan_out: 1,
        ..WorkflowCaps::default()
    };
    let allow = Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[linch::PermissionMode::SkipDangerous])
        .workspace_roots(&[dir.path().to_path_buf()])
        .unwrap()
        .workflow(Some(caps));
    let rpc = Rpc::remote(tenant("acme", allow));
    let sleep = tool_turn("c1", "Bash", json!({"command": "sleep 30"}));
    let mut config = scripted(
        vec![sleep, text_turn("x")],
        Some(dir.path()),
        "skip_dangerous",
    );
    config["namespace"] = json!("acme");
    let agent = rpc.open(config).await;
    start(&rpc, &agent, "w").await.unwrap();
    let first = call(&rpc, "w", "one", json!({"call_id": "a"}));
    let second = async {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let refused = call(&rpc, "w", "two", json!({"call_id": "b"})).await;
        let params = json!({"run_id": "w"});
        rpc.call(method::WORKFLOW_CANCEL, params).await.unwrap();
        refused
    };
    let (first, second) = tokio::join!(first, second);
    let (kind, data) = code_of_call(second);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::LIMIT_REACHED, json!("agent_calls_in_flight"))
    );
    assert!(first.is_err(), "the cancel ended the first call");
}

#[tokio::test]
async fn two_tenants_with_one_workflow_id_have_journals_of_their_own() {
    let Some(db) = TestDb::empty("wf_tenants").await else {
        return;
    };
    let caps = Some(WorkflowCaps::default());
    let acme = tenant_rpc(&db, "acme", caps);
    let globex = tenant_rpc(&db, "globex", caps);
    let a = acme
        .open(tenant_config(&db, "acme", vec![text_turn("acme's")]))
        .await;
    let g = globex
        .open(tenant_config(&db, "globex", vec![text_turn("globex's")]))
        .await;
    start(&acme, &a, "shared-id").await.unwrap();
    call(&acme, "shared-id", "same prompt", json!({}))
        .await
        .unwrap();
    finish(&acme, "shared-id", true).await;
    // The other tenant's workflow of that id is new, and the same prompt
    // runs on its own provider, not from acme's journal.
    let started = start(&globex, &g, "shared-id").await.unwrap();
    assert_eq!(
        (started["resumed"].clone(), started["records"].clone()),
        (json!(false), json!(0))
    );
    let theirs = call(&globex, "shared-id", "same prompt", json!({}))
        .await
        .unwrap();
    assert_eq!(
        (theirs["text"].clone(), theirs["replayed"].clone()),
        (json!("globex's"), json!(false))
    );
    // A tenant names no other namespace, so it cannot reach acme's journal.
    let mut stolen = tenant_config(&db, "acme", vec![]);
    stolen["namespace"] = json!("acme");
    let (kind, data) = code_of_call(globex.call(method::AGENT_OPEN, stolen).await);
    assert_eq!(
        (kind, data["kind"].clone()),
        (code::NOT_ALLOWED, json!("namespace"))
    );
    db.drop().await;
}

#[tokio::test]
async fn a_journal_failure_shows_a_tenant_no_database_text() {
    let Some(db) = TestDb::empty("wf_scrub").await else {
        return;
    };
    let rpc = tenant_rpc(&db, "acme", Some(WorkflowCaps::default()));
    let agent = rpc.open(tenant_config(&db, "acme", vec![])).await;
    start(&rpc, &agent, "w").await.unwrap();
    let begun = json!({"run_id": "w", "name": "s"});
    let begun = rpc.call(method::WORKFLOW_STEP_BEGIN, begun).await.unwrap();
    db.break_table("linch_ext_workflow_records").await;
    let params = json!({"run_id": "w", "ticket": begun["ticket"], "value": 1});
    let failed = rpc.call(method::WORKFLOW_STEP_COMPLETE, params).await;
    let ClientError::Rpc(error) = failed.unwrap_err() else {
        panic!("expected an error object")
    };
    assert_eq!(error.code, code::STORE);
    let text = format!("{} {:?}", error.message, error.data);
    assert!(
        !text.contains("127.0.0.1") && !text.contains("linch_ext"),
        "{text}"
    );
    assert_eq!(error.message, "the workflow journal failed");
    db.drop().await;
}

#[tokio::test]
async fn a_call_cut_off_by_a_dead_client_continues_its_run_on_the_next() {
    let Some(db) = TestDb::empty("wf_mid_call").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let config = |turns| {
        let mut config = scripted(turns, Some(dir.path()), "skip_dangerous");
        config["stores"] = json!({"type": "postgres", "dsn": db.dsn, "lease_ttl_ms": 1000});
        config
    };
    {
        let rpc = Rpc::new();
        let sleep = tool_turn("c1", "Bash", json!({"command": "sleep 30"}));
        let agent = rpc.open(config(vec![sleep])).await;
        start(&rpc, &agent, "w").await.unwrap();
        let waiting = call(&rpc, "w", "scan", json!({}));
        tokio::time::timeout(Duration::from_secs(2), waiting)
            .await
            .expect_err("the call is still running when the client goes");
    }
    // The next client has the one turn the run still needs: the call
    // continues the run the first one started, and never repeats its work.
    let rpc = Rpc::new();
    let agent = rpc.open(config(vec![text_turn("done")])).await;
    start_when_free(&rpc, &agent, "w").await;
    let finished = call(&rpc, "w", "scan", json!({})).await.unwrap();
    assert_eq!(
        (finished["text"].clone(), finished["replayed"].clone()),
        (json!("done"), json!(false))
    );
    // One run, begun by the first client and resumed by the second.
    let params = json!({"agent": agent, "run_id": finished["session_id"]});
    let events = rpc.call(method::RUN_EVENTS, params).await.unwrap();
    let inits: Vec<_> = (events["events"].as_array().unwrap().iter())
        .filter(|envelope| envelope["event"]["type"] == "init")
        .map(|envelope| envelope["event"]["resumed"] == json!(true))
        .collect();
    assert_eq!(inits, [false, true]);
    finish(&rpc, "w", true).await;
    db.drop().await;
}

#[tokio::test]
async fn a_call_can_fork_the_session_an_earlier_call_returned() {
    let Some(db) = TestDb::empty("wf_fork").await else {
        return;
    };
    let rpc = Rpc::new();
    let turns = vec![
        text_turn("seven"),
        text_turn("seven again"),
        text_turn("fresh"),
    ];
    let agent = rpc.open(on_postgres(&db, turns)).await;
    start(&rpc, &agent, "w").await.unwrap();
    let first = call(&rpc, "w", "remember 7", json!({})).await.unwrap();
    let extra = json!({"fork": first["session_id"]});
    let second = call(&rpc, "w", "what was it?", extra).await.unwrap();
    assert_eq!(second["replayed"], json!(false));
    assert_ne!(second["session_id"], first["session_id"]);
    // The fork is part of the call's identity.
    let alone = call(&rpc, "w", "what was it?", json!({})).await.unwrap();
    assert_eq!(alone["replayed"], json!(false));
    // A session that does not exist cannot be forked.
    let missing = call(&rpc, "w", "x", json!({"fork": "nope"})).await;
    assert_eq!(code_of_call(missing).0, code::WORKFLOW_CALL_FAILED);
    db.drop().await;
}

/// Guessing is no way in: a tenant that knows another tenant's workflow id
/// and the session of one of its calls reads no journal, forks no session
/// and resumes no run, because every store key is the tenant's namespace.
#[tokio::test]
async fn a_tenant_that_guesses_another_tenants_ids_reaches_nothing() {
    let Some(db) = TestDb::empty("wf_guess").await else {
        return;
    };
    let caps = Some(WorkflowCaps::default());
    let acme = tenant_rpc(&db, "acme", caps);
    let globex = tenant_rpc(&db, "globex", caps);
    let a = acme
        .open(tenant_config(&db, "acme", vec![text_turn("acme secret")]))
        .await;
    let turns = vec![text_turn("globex's"), text_turn("globex's again")];
    let g = globex.open(tenant_config(&db, "globex", turns)).await;
    start(&acme, &a, "plan").await.unwrap();
    let theirs = call(&acme, "plan", "the plan", json!({})).await.unwrap();
    finish(&acme, "plan", true).await;
    let session = theirs["session_id"].clone();
    // The status of acme's workflow, by its id, from globex's agent.
    let status = json!({"agent": g, "run_id": "plan"});
    let (kind, _) = code_of_call(globex.call(method::WORKFLOW_STATUS, status).await);
    assert_eq!(kind, code::NOT_FOUND);
    // The events of the call's run (its id is its session), likewise.
    let events = json!({"agent": g, "run_id": session});
    let (kind, _) = code_of_call(globex.call(method::RUN_EVENTS, events).await);
    assert_eq!(kind, code::NOT_FOUND);
    // A fork of the call's session reads nothing of acme's conversation.
    start(&globex, &g, "plan").await.unwrap();
    let fork = json!({"fork": session});
    let stolen = call(&globex, "plan", "show it", fork).await;
    assert_eq!(code_of_call(stolen).0, code::WORKFLOW_CALL_FAILED);
    // The same prompt under the same id runs on globex's provider.
    let own = call(&globex, "plan", "the plan", json!({})).await.unwrap();
    assert_eq!(
        (own["text"].clone(), own["replayed"].clone()),
        (json!("globex's"), json!(false))
    );
    db.drop().await;
}

/// RED: a workflow's calls act for no principal, so a permission rule keyed
/// on a role (here: deny `Glob` to `guest`) does not reach them, while the
/// same call in a run started for the guest is denied. `workflow.start`
/// should take the principal its calls act for.
#[tokio::test]
async fn a_workflow_started_for_a_principal_runs_its_calls_under_that_principals_rules() {
    let dir = tempfile::tempdir().unwrap();
    let turns = vec![
        tool_turn("c1", "Glob", json!({"pattern": "*"})),
        text_turn("done"),
        tool_turn("c2", "Glob", json!({"pattern": "*"})),
        text_turn("done"),
    ];
    let mut config = scripted(turns, Some(dir.path()), "default");
    let deny = json!({"type": "tool", "tool": "Glob", "decision": "deny", "role": "guest"});
    config["permissions"]["layers"] = json!([{"name": "roles", "rules": [deny]}]);
    let mut rpc = Rpc::new();
    let agent = rpc.open(config).await;
    let guest = json!({"tenant": "acme", "user": "gus", "roles": ["guest"]});
    let denied = |events: &[linch::Envelope]| {
        (events.iter())
            .filter_map(|envelope| match &envelope.event {
                linch::Event::ToolCallEnd(end) => Some(end.is_error()),
                _ => None,
            })
            .collect::<Vec<_>>()
    };
    // The rule works for a run started for the guest.
    let params = json!({"agent": agent, "session_id": "direct", "prompt": "go",
                        "principal": guest});
    let run_id = rpc.accepted(method::RUN_START, params).await;
    let ended = rpc.until_end(&run_id).await;
    assert_eq!(denied(&ended.envelopes), [true], "the control run");
    // The same agent in a workflow started for the guest.
    let params = json!({"agent": agent, "run_id": "w", "principal": guest});
    rpc.call(method::WORKFLOW_START, params).await.unwrap();
    let result = call(&rpc, "w", "go", json!({})).await.unwrap();
    let params = json!({"agent": agent, "run_id": result["session_id"]});
    let events = rpc.call(method::RUN_EVENTS, params).await.unwrap();
    let events: linch_rpc::protocol::Events = serde_json::from_value(events).unwrap();
    assert_eq!(denied(&events.events), [true], "the workflow's call");
}
