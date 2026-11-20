//! `eval.run` and `run.report`: cost, tokens and timings per case, the
//! stored run behind each case, and the checks around both.

mod common;

use std::future::pending;

use common::pg::TestDb;
use common::*;
use linch::Usage;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::method;
use linch_rpc::protocol::monitor::{EVAL_RUN, RUN_REPORT};
use linch_rpc::{Allowlists, ErrorObject, code};
use serde_json::{Value, json};
use tokio::sync::mpsc;

/// Cases the tenant cap lets one `eval.run` take (the server's own cap, 256).
const MAX_EVAL_CASES: usize = 256;

/// An answer of `text` that used 1000 input and 100 output tokens.
fn priced_text(text: &str) -> linch::ScriptedTurn {
    let usage = Usage {
        input_tokens: 1_000,
        output_tokens: 100,
        ..Usage::default()
    };
    TurnBuilder::new(MODEL).text(text).usage(usage).build()
}

/// A config pricing the scripted model at 1 USD per million input tokens
/// and 2 per million output.
fn priced(turns: Vec<linch::ScriptedTurn>) -> Value {
    let mut config = scripted(turns, None, "");
    config["pricing"] = json!({MODEL: {"input": 1.0, "output": 2.0}});
    config
}

#[tokio::test]
async fn an_eval_scores_each_case_and_reports_cost_tokens_and_duration() {
    let rpc = Rpc::new();
    let turns = vec![priced_text("It is Paris."), priced_text("Munich")];
    let agent = rpc.open(priced(turns)).await;
    let params = json!({
        "agent": agent,
        "cases": [
            {"prompt": "Capital of France?", "expected": "Paris"},
            {"prompt": "Capital of Germany?", "expected": "Berlin"},
        ],
        "scorers": [
            {"type": "text_contains"},
            {"type": "cost_under", "budget_usd": 0.01},
            {"type": "run_completed"},
        ],
    });
    let report = rpc.call(EVAL_RUN, params).await.unwrap();
    assert_eq!(
        (report["total"].clone(), report["passed"].clone()),
        (json!(2), json!(1))
    );
    assert_eq!(report["pass_rate"], 0.5);
    assert_eq!(report["usage"]["input_tokens"], 2_000);
    assert_eq!(report["usage"]["output_tokens"], 200);
    let each = 1_000.0 * 1e-6 + 100.0 * 2e-6;
    assert_eq!(report["total_cost_usd"], each + each);
    assert!(report["duration_ms"].is_u64());
    assert!(
        report["markdown"]
            .as_str()
            .unwrap()
            .starts_with("# Linch Eval Report")
    );

    let first = &report["cases"][0];
    assert_eq!(first["passed"], true);
    assert_eq!(first["total_cost_usd"], each);
    assert_eq!(
        first["scores"][1],
        json!({"name": "cost_under:0.01", "verdict": true})
    );
    assert!(first.get("events").is_none());
    assert_eq!(report["cases"][1]["scores"][0]["verdict"], false);

    // The run behind a case reports through `run.report`.
    let run_id = first["run_id"].as_str().unwrap();
    let params = json!({"agent": agent, "run_id": run_id});
    let one = rpc.call(RUN_REPORT, params).await.unwrap();
    assert_eq!(one["report"]["run_id"], run_id);
    assert_eq!(one["report"]["status"], "completed");
    assert_eq!(one["report"]["summary"]["usage"]["input_tokens"], 1_000);
    assert_eq!(one["report"]["final"]["total_cost_usd"], each);
    assert_eq!(one["report"]["timeline"], json!([]));
    assert!(
        one["markdown"]
            .as_str()
            .unwrap()
            .starts_with("# Linch Run Report")
    );
    let params = json!({"agent": agent, "run_id": run_id, "timeline": true});
    let full = rpc.call(RUN_REPORT, params).await.unwrap();
    assert_eq!(
        full["report"]["timeline"].as_array().unwrap().len(),
        full["report"]["event_count"].as_u64().unwrap() as usize
    );
}

#[tokio::test]
async fn an_eval_can_carry_the_events_of_each_case() {
    let rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("hi")], None, "")).await;
    let params = json!({"agent": agent, "cases": [{"prompt": "q"}], "include_events": true});
    let report = rpc.call(EVAL_RUN, params).await.unwrap();
    let events = report["cases"][0]["events"].as_array().unwrap();
    assert_eq!(events[0]["event"]["type"], "init");
    // Without a price the cost is unknown, not zero.
    assert_eq!(report["total_cost_usd"], Value::Null);
}

#[tokio::test]
async fn bad_eval_and_report_params_are_refused_with_their_code() {
    let rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("hi")], None, "")).await;
    let bad = [
        json!({"agent": agent, "cases": [{"prompt": ""}]}),
        json!({"agent": agent, "cases": [], "scorers": [{"type": "no_such"}]}),
        json!({"agent": agent, "cases": [], "scorers": [{"type": "tool_called", "tool": ""}]}),
        json!({"agent": agent, "cases": [], "scorers": [{"type": "cost_under"}]}),
        json!({"agent": agent, "cases": [], "extra": 1}),
    ];
    for params in bad {
        let error = rpc.call(EVAL_RUN, params.clone()).await.unwrap_err();
        assert_eq!(code_of(error), code::INVALID_PARAMS, "{params}");
    }
    let missing = json!({"agent": "agent-99", "cases": []});
    let error = rpc.call(EVAL_RUN, missing).await.unwrap_err();
    assert_eq!(code_of(error), code::AGENT_NOT_FOUND);
    let params = json!({"agent": agent, "run_id": "no-such-run"});
    let error = rpc.call(RUN_REPORT, params).await.unwrap_err();
    assert_eq!(code_of(error), code::NOT_FOUND);
    let params = json!({"agent": "agent-99", "run_id": "r"});
    let error = rpc.call(RUN_REPORT, params).await.unwrap_err();
    assert_eq!(code_of(error), code::AGENT_NOT_FOUND);
}

#[tokio::test]
async fn a_tenants_eval_takes_a_bounded_number_of_cases() {
    let allow = Allowlists::new().scripted_provider(true);
    let rpc = Rpc::remote(tenant("acme", allow));
    let mut config = scripted(vec![text_turn("hi")], None, "");
    config["namespace"] = json!("acme");
    let agent = rpc.open(config).await;
    let cases =
        |count: usize| json!({"agent": agent, "cases": vec![json!({"prompt": "q"}); count]});
    // The cap's own count runs; one more is refused before any case runs.
    let report = rpc.call(EVAL_RUN, cases(MAX_EVAL_CASES)).await.unwrap();
    assert_eq!(report["total"], MAX_EVAL_CASES);
    let error = rpc
        .call(EVAL_RUN, cases(MAX_EVAL_CASES + 1))
        .await
        .unwrap_err();
    assert_eq!(code_of(error), code::INVALID_PARAMS);
}

/// The runs of an eval are claimed by the server like any run it drives,
/// not under the library's default owner name.
#[tokio::test]
async fn an_evals_runs_are_claimed_by_the_server() {
    let Some(db) = TestDb::empty("eval_owner").await else {
        return;
    };
    let mut rpc = Rpc::new();
    let mut config = scripted(vec![text_turn("one"), text_turn("two")], None, "");
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    let agent = rpc.open(config).await;
    let params = json!({"agent": agent, "cases": [{"prompt": "q"}]});
    let report = rpc.call(EVAL_RUN, params).await.unwrap();
    let evaluated = report["cases"][0]["run_id"].as_str().unwrap().to_owned();
    let started = rpc.start(&agent, "s1", "go").await;
    rpc.until_end(&started).await;
    let owner = db.run_owner(started.as_str()).await;
    assert_eq!(db.run_owner(&evaluated).await, owner);
    assert_ne!(owner, "linch");
    db.drop().await;
}

#[tokio::test]
async fn an_agent_cannot_be_closed_while_an_eval_runs_on_it() {
    let (arrived, mut calls) = mpsc::unbounded_channel();
    let rpc = Rpc::with_handler(handler(move |_, _, _| {
        let _ = arrived.send(());
        pending::<Result<Value, ErrorObject>>()
    }));
    let turns = vec![
        tool_turn("call_1", "lookup", json!({"q": "x"})),
        text_turn("unused"),
    ];
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let eval = rpc.call(
        EVAL_RUN,
        json!({"agent": agent, "cases": [{"prompt": "go"}]}),
    );
    let closing = async {
        calls.recv().await.expect("the tool call arrives");
        rpc.call(method::AGENT_CLOSE, json!({"agent": agent})).await
    };
    tokio::select! {
        _ = eval => panic!("the eval cannot finish"),
        busy = closing => assert_eq!(code_of(busy.unwrap_err()), code::RUN_BUSY),
    }
}
