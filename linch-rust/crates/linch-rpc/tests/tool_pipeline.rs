//! The `tool_execute` waterfall and `post_tool_use` mutation over the
//! wire: a `before` call that proceeds or vetoes, an `after` call that
//! sees the result, and a post callback that changes part of the result.

mod common;

use std::sync::{Arc, Mutex};

use common::*;
use linch::Event;
use serde_json::{Value, json};

/// A client that runs host tools; each callback is answered by `answer`
/// from the point and the request's `phase`. Every request is kept.
fn client(answer: fn(&str, &Value) -> Value) -> (Rpc, Arc<Mutex<Vec<Value>>>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&seen);
    let rpc = Rpc::with_handler(handler(move |method, params, _cancel| {
        let log = Arc::clone(&log);
        async move {
            let params = Value::Object(params);
            log.lock()
                .unwrap()
                .push(json!({"method": method, "params": params}));
            if method == linch_rpc::protocol::method::TOOL_CALL {
                return Ok(json!({"value": "ran"}));
            }
            let point = params["point"].as_str().unwrap();
            Ok(answer(point, &params["request"]))
        }
    }));
    (rpc, seen)
}

fn steps(seen: &Arc<Mutex<Vec<Value>>>) -> Vec<String> {
    (seen.lock().unwrap().iter())
        .map(|r| match r["params"]["point"].as_str() {
            Some("tool_execute") => format!("tool_execute:{}", r["params"]["request"]["phase"]),
            Some(point) => point.to_owned(),
            None => r["method"].as_str().unwrap().to_owned(),
        })
        .collect()
}

fn turns() -> Vec<linch::ScriptedTurn> {
    vec![
        tool_turn("call_1", "lookup", json!({"q": "cats"})),
        text_turn("OK."),
    ]
}

fn config(callbacks: Value) -> Value {
    hosted(
        turns(),
        "default",
        json!({"tools": [host_tool("lookup", "read")], "callbacks": callbacks}),
    )
}

async fn run(rpc: &mut Rpc, callbacks: Value) -> Ended {
    let agent = rpc.open(config(callbacks)).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    ended
}

fn execute(id: &str, extra: Value) -> Value {
    let mut spec = json!({"point": "tool_execute", "policy_id": id, "policy_version": 1});
    spec.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    spec
}

#[tokio::test]
async fn a_tool_execute_callback_that_proceeds_lets_the_tool_run() {
    let (mut rpc, seen) = client(|_, _| json!({"action": "next"}));
    let ended = run(&mut rpc, json!([execute("meter", json!({}))])).await;
    assert_eq!(steps(&seen), ["tool_execute:\"before\"", "tool.call"]);
    let request = seen.lock().unwrap()[0]["params"]["request"].clone();
    assert_eq!(request["tool_name"], "lookup");
    assert_eq!(request["attempt"], 1);
    assert_eq!(request["input"], json!({"q": "cats"}));
    assert!(tool_results(&ended)[0].to_string().contains("ran"));
}

#[tokio::test]
async fn an_after_call_sees_the_result_and_may_replace_it() {
    let (mut rpc, seen) = client(|_, request| match request["phase"].as_str() {
        Some("before") => json!({"action": "next", "after": true}),
        _ => json!({"action": "replace", "result": {"output": {"value": "wrapped"}}}),
    });
    let ended = run(&mut rpc, json!([execute("wrap", json!({}))])).await;
    assert_eq!(
        steps(&seen),
        [
            "tool_execute:\"before\"",
            "tool.call",
            "tool_execute:\"after\""
        ]
    );
    let requests = seen.lock().unwrap();
    let (before, after) = (
        &requests[0]["params"]["request"],
        &requests[2]["params"]["request"],
    );
    assert_eq!(before["token"], after["token"], "one token pairs the calls");
    assert_eq!(after["result"], json!({"output": {"value": "ran"}}));
    drop(requests);
    let results = tool_results(&ended)[0].to_string();
    assert!(
        results.contains("wrapped") && !results.contains("ran"),
        "{results}"
    );
}

#[tokio::test]
async fn a_veto_answers_without_running_the_tool() {
    let (mut rpc, seen) =
        client(|_, _| json!({"action": "veto", "result": {"error": {"message": "over quota"}}}));
    let ended = run(&mut rpc, json!([execute("quota", json!({}))])).await;
    assert_eq!(steps(&seen), ["tool_execute:\"before\""]);
    let result = &tool_results(&ended)[0];
    assert_eq!(result["is_error"], true);
    assert!(result.to_string().contains("over quota"), "{result}");
}

#[tokio::test]
async fn a_failing_callback_vetoes_when_closed_and_proceeds_when_open() {
    let (mut rpc, seen) = client(|_, _| json!({"action": "continue"}));
    let ended = run(&mut rpc, json!([execute("bad", json!({}))])).await;
    let results = tool_results(&ended);
    assert_eq!(results[0]["is_error"], true);
    assert!(results[0].to_string().contains("hook `bad` failed"));
    assert_eq!(steps(&seen), ["tool_execute:\"before\""]);

    let (mut rpc, seen) = client(|_, _| json!({"action": "continue"}));
    let open = execute("bad", json!({"fail_mode": "open"}));
    let ended = run(&mut rpc, json!([open])).await;
    assert_ne!(tool_results(&ended)[0]["is_error"], true);
    assert_eq!(steps(&seen), ["tool_execute:\"before\"", "tool.call"]);
}

#[tokio::test]
async fn a_matcher_keeps_other_tools_out_of_the_waterfall() {
    let (mut rpc, seen) = client(|_, _| json!({"action": "next"}));
    let layer = execute("other", json!({"matcher": ["Write"]}));
    run(&mut rpc, json!([layer])).await;
    assert_eq!(steps(&seen), ["tool.call"]);
}

#[tokio::test]
async fn a_post_tool_use_callback_mutates_content_and_the_error_flag() {
    let (mut rpc, seen) =
        client(|_, _| json!({"action": "mutate", "content": "[redacted]", "is_error": true}));
    let post = json!({"point": "post_tool_use", "policy_id": "scrub", "policy_version": 1});
    let ended = run(&mut rpc, json!([post])).await;
    assert_eq!(steps(&seen), ["tool.call", "post_tool_use"]);
    let result = &tool_results(&ended)[0];
    assert_eq!(result["is_error"], true);
    assert!(result.to_string().contains("[redacted]"), "{result}");
    let end = (ended.envelopes.iter())
        .find_map(|e| match &e.event {
            Event::ToolCallEnd(end) => Some(end.clone()),
            _ => None,
        })
        .unwrap();
    assert!(end.result.is_error, "the log holds the mutated result");
}

#[tokio::test]
async fn an_unset_waterfall_adds_no_callback_traffic() {
    let (mut rpc, seen) = client(|_, _| json!({"action": "next"}));
    run(&mut rpc, json!([])).await;
    assert_eq!(steps(&seen), ["tool.call"]);
}
