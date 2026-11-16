//! A connection that ends while a callback is waiting for its answer
//! leaves the run where it stood: the callback's failure is not recorded as
//! its answer, and `run.resume` on a new connection asks again.

mod common;

use std::future::pending;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::Event;
use linch::events::kinds::ResultSubtype;
use linch_rpc::protocol::method;
use serde_json::{Value, json};
use tokio::sync::Notify;

type Seen = Arc<Mutex<Vec<String>>>;

/// A client that records each server request's method (and point), runs
/// host tools, and answers `hook.call` with `answer`; `None` hangs.
fn client(answer: Option<Value>, asked: Arc<Notify>) -> (Rpc, Seen) {
    let seen = Seen::default();
    let log = seen.clone();
    let rpc = Rpc::with_handler(handler(move |method, params, _cancel| {
        let (log, answer, asked) = (log.clone(), answer.clone(), asked.clone());
        async move {
            let point = params.get("point").and_then(Value::as_str);
            log.lock().unwrap().push(match point {
                Some(point) => format!("hook.call:{point}"),
                None => method.to_owned(),
            });
            if method == method::TOOL_CALL {
                return Ok(json!({"value": "ran"}));
            }
            asked.notify_one();
            match answer {
                Some(answer) => Ok(answer),
                None => pending().await,
            }
        }
    }));
    (rpc, seen)
}

/// The scripted provider keeps its place only inside one agent, so the
/// resuming agent is given the turns the first one had not used.
fn config(dsn: &str, turns: Vec<linch::ScriptedTurn>) -> Value {
    let approver = json!({"point": "pre_tool_use", "policy_id": "gate",
                          "policy_version": 1, "fail_mode": "closed"});
    let mut config = hosted(
        turns,
        "default",
        json!({"tools": [host_tool("poke", "read")], "callbacks": [approver]}),
    );
    config["stores"] = json!({"type": "postgres", "dsn": dsn});
    config
}

#[tokio::test]
async fn a_connection_lost_mid_callback_is_not_the_callbacks_answer() {
    let Some(db) = TestDb::empty("disconnect_mid_hook").await else {
        return;
    };
    let asked = Arc::new(Notify::new());
    let (first, seen) = client(None, asked.clone());
    let agent = first
        .open(config(
            &db.dsn,
            vec![tool_turn("call_1", "poke", json!({})), text_turn("OK.")],
        ))
        .await;
    let run_id = first.start(&agent, "s1", "Go.").await;
    tokio::time::timeout(Duration::from_secs(10), asked.notified())
        .await
        .expect("the callback was asked");
    assert_eq!(*seen.lock().unwrap(), ["hook.call:pre_tool_use"]);

    // The connection ends while the callback waits.
    drop(first);
    tokio::time::sleep(Duration::from_millis(500)).await;

    // A new connection resumes the run; the callback is asked again and
    // this time allows the call.
    let (mut second, seen) = client(Some(json!({"action": "continue"})), Arc::new(Notify::new()));
    let agent = second.open(config(&db.dsn, vec![text_turn("OK.")])).await;
    let params = json!({"agent": agent, "run_id": run_id});
    assert_eq!(second.accepted(method::RUN_RESUME, params).await, run_id);
    let ended = second.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    assert!(
        matches!(ended.last(), Event::Result(r) if r.subtype == ResultSubtype::Success),
        "{:?}",
        ended.last()
    );
    assert_eq!(
        *seen.lock().unwrap(),
        ["hook.call:pre_tool_use", "tool.call"],
        "asked again, then the tool ran"
    );
    let results = tool_results(&ended);
    assert!(!results[0].to_string().contains("gate"), "{results:?}");
    db.drop().await;
}
