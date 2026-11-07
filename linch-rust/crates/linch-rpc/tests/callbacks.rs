//! Intercept callbacks at the tool and turn points: declared in `agent.open`,
//! answered by the client through `hook.call`, filtered by matchers in the
//! server, bounded by timeouts and fail modes, and part of the contract.

mod common;

use std::future::pending;
use std::sync::{Arc, Mutex};

use common::*;
use linch::Event;
use linch::events::kinds::ResultSubtype;
use linch_rpc::protocol::{AgentOpened, method};
use linch_rpc::{ClientError, code};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

/// Every request the client got, and the cancel token of each it left
/// hanging.
#[derive(Clone, Default)]
struct Log {
    requests: Arc<Mutex<Vec<Value>>>,
    hung: Arc<Mutex<Vec<CancellationToken>>>,
}

impl Log {
    /// `tool.call`, or `hook.call:<point>`, per request in arrival order.
    fn methods(&self) -> Vec<String> {
        let requests = self.requests.lock().unwrap();
        (requests.iter())
            .map(|r| match r["params"]["point"].as_str() {
                Some(point) => format!("hook.call:{point}"),
                None => r["method"].as_str().unwrap().to_owned(),
            })
            .collect()
    }

    fn params(&self, index: usize) -> Value {
        self.requests.lock().unwrap()[index]["params"].clone()
    }

    fn hung(&self) -> Vec<CancellationToken> {
        self.hung.lock().unwrap().clone()
    }
}

/// A client that runs host tools and answers each callback by its point
/// from `answers`; a `null` answer hangs.
fn client(answers: Value) -> (Rpc, Log) {
    let log = Log::default();
    let seen = log.clone();
    let rpc = Rpc::with_handler(handler(move |method, params, cancel| {
        let (seen, answers) = (seen.clone(), answers.clone());
        async move {
            let params = Value::Object(params);
            let request = json!({"method": method, "params": params});
            seen.requests.lock().unwrap().push(request);
            if method == method::TOOL_CALL {
                return Ok(json!({"value": format!("ran with {}", params["input"])}));
            }
            match &answers[params["point"].as_str().unwrap()] {
                Value::Null => {
                    seen.hung.lock().unwrap().push(cancel);
                    pending().await
                }
                answer => Ok(answer.clone()),
            }
        }
    }));
    (rpc, log)
}

/// Open an agent with `tools` and `callbacks`, run it once, and return
/// what the run sent.
async fn run(rpc: &mut Rpc, turns: Vec<linch::ScriptedTurn>, extra: Value) -> Ended {
    let agent = rpc.open(hosted(turns, "default", extra)).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    ended
}

fn lookup_turns() -> Vec<linch::ScriptedTurn> {
    vec![
        tool_turn("call_1", "lookup", json!({"q": "cats"})),
        text_turn("OK."),
    ]
}

fn finished(ended: &Ended) -> bool {
    matches!(ended.last(), Event::Result(r) if r.subtype == ResultSubtype::Success)
}

#[tokio::test]
async fn a_pre_tool_use_callback_blocks_a_call() {
    let (mut rpc, log) = client(json!({
        "pre_tool_use": {"action": "block", "reason": "not on Fridays"},
    }));
    let extra = json!({
        "tools": [host_tool("lookup", "read")],
        "callbacks": [{"point": "pre_tool_use", "policy_id": "friday", "policy_version": 1,
                       "matcher": ["look*"]}],
    });
    let ended = run(&mut rpc, lookup_turns(), extra).await;
    let results = tool_results(&ended);
    assert!(
        results[0].to_string().contains("not on Fridays"),
        "{results:?}"
    );
    assert_eq!(results[0]["is_error"], true);
    assert!(finished(&ended));
    assert_eq!(
        log.methods(),
        ["hook.call:pre_tool_use"],
        "the tool never ran"
    );
    let params = log.params(0);
    assert_eq!(params["policy_id"], "friday");
    assert_eq!(params["agent"], "agent-1");
    let request = &params["request"];
    assert_eq!(request["tool_name"], "lookup");
    assert_eq!(request["tool_use_id"], "call_1");
    assert_eq!(request["input"], json!({"q": "cats"}));
}

#[tokio::test]
async fn callbacks_rewrite_the_input_and_replace_the_result() {
    let (mut rpc, log) = client(json!({
        "pre_tool_use": {"action": "rewrite", "input": {"q": "dogs"}},
        "post_tool_use": {"action": "replace", "result": {"output": {"value": "redacted"}}},
    }));
    let extra = json!({
        "tools": [host_tool("lookup", "read")],
        "callbacks": [
            {"point": "pre_tool_use", "policy_id": "rewriter", "policy_version": 1},
            {"point": "post_tool_use", "policy_id": "redactor", "policy_version": 1,
             "matcher": ["lookup"]},
        ],
    });
    let ended = run(&mut rpc, lookup_turns(), extra).await;
    let results = tool_results(&ended).remove(0).to_string();
    assert!(
        results.contains("redacted") && !results.contains("dogs"),
        "{results}"
    );
    assert_eq!(
        log.methods(),
        [
            "hook.call:pre_tool_use",
            "tool.call",
            "hook.call:post_tool_use"
        ]
    );
    assert_eq!(log.params(1)["input"], json!({"q": "dogs"}));
    let ran = json!({"output": {"value": "ran with {\"q\":\"dogs\"}"}});
    assert_eq!(log.params(2)["request"]["result"], ran);
}

#[tokio::test]
async fn a_permission_request_callback_denies_a_call() {
    let (mut rpc, log) = client(json!({
        "permission_request": {"decision": "deny", "reason": "no exec today"},
    }));
    let extra = json!({
        "tools": [host_tool("poke", "exec")],
        "callbacks": [{"point": "permission_request", "policy_id": "approver",
                       "policy_version": 1}],
    });
    let turns = vec![tool_turn("call_1", "poke", json!({})), text_turn("OK.")];
    let ended = run(&mut rpc, turns, extra).await;
    let results = tool_results(&ended);
    assert!(
        results[0].to_string().contains("no exec today"),
        "{results:?}"
    );
    assert!(finished(&ended), "denied, not suspended");
    assert_eq!(log.methods(), ["hook.call:permission_request"]);
    assert_eq!(log.params(0)["request"]["tool_name"], "poke");
}

#[tokio::test]
async fn a_permission_request_callback_fails_closed_on_timeout() {
    let (mut rpc, log) = client(json!({"permission_request": null}));
    let approver = json!({"point": "permission_request", "policy_id": "approver",
                          "policy_version": 1, "timeout_ms": 50});
    let extra = json!({"tools": [host_tool("poke", "exec")], "callbacks": [approver]});
    let turns = vec![tool_turn("call_1", "poke", json!({})), text_turn("OK.")];
    let ended = run(&mut rpc, turns, extra).await;
    let results = tool_results(&ended);
    assert!(
        results[0].to_string().contains("hook `approver` timed out"),
        "{results:?}"
    );
    assert!(finished(&ended), "denied, not suspended");
    assert!(log.hung()[0].is_cancelled(), "the client was told");

    // Overridden to fail open, the timeout leaves the call asking.
    let (mut rpc, log) = client(json!({"permission_request": null}));
    let mut lenient = approver;
    lenient["fail_mode"] = json!("open");
    let extra = json!({"tools": [host_tool("poke", "exec")], "callbacks": [lenient]});
    let turns = vec![tool_turn("call_1", "poke", json!({})), text_turn("OK.")];
    let ended = run(&mut rpc, turns, extra).await;
    assert!(
        matches!(ended.last(), Event::Suspended(_)),
        "{:?}",
        ended.last()
    );
    assert!(log.hung()[0].is_cancelled(), "the client was told");
}

#[tokio::test]
async fn a_matcher_miss_sends_nothing_over_the_connection() {
    let (mut rpc, log) = client(json!({
        "pre_tool_use": {"action": "block", "reason": "matched"},
        "permission_request": {"decision": "deny", "reason": "matched"},
        "post_tool_use": {"action": "replace", "result": {"output": {"value": "matched"}}},
    }));
    let extra = json!({
        "tools": [host_tool("lookup", "read")],
        "callbacks": [
            {"point": "pre_tool_use", "policy_id": "a", "policy_version": 1,
             "matcher": ["Write*", "mcp__*"]},
            {"point": "permission_request", "policy_id": "b", "policy_version": 1,
             "matcher": ["lookups"]},
            {"point": "post_tool_use", "policy_id": "c", "policy_version": 1,
             "matcher": ["?ookup?"]},
        ],
    });
    let ended = run(&mut rpc, lookup_turns(), extra).await;
    assert_eq!(log.methods(), ["tool.call"]);
    assert!(!tool_results(&ended)[0].to_string().contains("matched"));
}

#[tokio::test]
async fn the_contract_changes_with_a_callback_policy() {
    let rpc = Rpc::new();
    async fn contract(rpc: &Rpc, callbacks: Value) -> String {
        let params = hosted(vec![], "default", json!({"callbacks": callbacks}));
        let opened: AgentOpened = rpc.client.call(method::AGENT_OPEN, &params).await.unwrap();
        opened.contract
    }
    let policy = |point: &str, id: &str, version: u32| json!([{"point": point, "policy_id": id, "policy_version": version}]);
    let none = contract(&rpc, json!([])).await;
    let v1 = contract(&rpc, policy("pre_tool_use", "p", 1)).await;
    assert_ne!(none, v1);
    assert_eq!(v1, contract(&rpc, policy("pre_tool_use", "p", 1)).await);
    assert_ne!(v1, contract(&rpc, policy("pre_tool_use", "p", 2)).await);
    assert_ne!(v1, contract(&rpc, policy("pre_tool_use", "q", 1)).await);
    // A turn point counts too.
    assert_ne!(v1, contract(&rpc, policy("post_turn", "p", 1)).await);
}

#[tokio::test]
async fn a_callback_declaration_is_checked_at_open() {
    let rpc = Rpc::new();
    let base = json!({"point": "pre_tool_use", "policy_id": "p", "policy_version": 1});
    let with = |key: &str, value: Value| {
        let mut callback = base.clone();
        callback[key] = value;
        callback
    };
    let mut no_version = base.clone();
    no_version.as_object_mut().unwrap().remove("policy_version");
    let invalid = [
        with("point", json!("after_everything")),
        with("policy_id", json!("  ")),
        with("matcher", json!([])),
        with("timeout_ms", json!(0)),
        with("extra", json!(1)),
        no_version,
        json!({"point": "stop", "policy_id": "p", "policy_version": 1, "matcher": ["*"]}),
        json!({"point": "post_turn", "policy_id": "p", "policy_version": 1,
               "fail_mode": "closed"}),
    ];
    for callback in invalid {
        let text = callback.to_string();
        let params = hosted(vec![], "default", json!({"callbacks": [callback]}));
        let error = rpc.call(method::AGENT_OPEN, params).await.unwrap_err();
        assert!(
            matches!(&error, ClientError::Rpc(e) if e.code == code::INVALID_PARAMS),
            "{text}: {error:?}"
        );
    }
}

#[tokio::test]
async fn turn_point_callbacks_cross_the_connection() {
    let (mut rpc, log) = client(json!({
        "user_prompt_submit": {"action": "rewrite",
                               "content": [{"type": "text", "text": "Go, politely."}]},
        "stop": {"action": "allow"},
    }));
    let extra = json!({"callbacks": [
        {"point": "user_prompt_submit", "policy_id": "polite", "policy_version": 1},
        {"point": "stop", "policy_id": "done", "policy_version": 1},
    ]});
    let ended = run(&mut rpc, vec![text_turn("Done.")], extra).await;
    assert!(finished(&ended));
    assert_eq!(
        log.methods(),
        ["hook.call:user_prompt_submit", "hook.call:stop"]
    );
    assert_eq!(log.params(0)["request"]["content"][0]["text"], "Go.");
    assert_eq!(log.params(1)["request"]["text"], "Done.");
    let prompt = (ended.envelopes.iter())
        .find_map(|envelope| match &envelope.event {
            Event::User(user) => Some(serde_json::to_string(&user.message).unwrap()),
            _ => None,
        })
        .expect("the prompt is recorded");
    assert!(prompt.contains("Go, politely."), "{prompt}");
    // Each callback's record arrives as a stream-only `hook` envelope.
    let hooks = ended.kinds().iter().filter(|kind| *kind == "hook").count();
    assert_eq!(hooks, 2, "{:?}", ended.kinds());

    let (mut rpc, _) = client(json!({
        "user_prompt_submit": {"action": "block", "reason": "no prompts today"},
    }));
    let extra = json!({"callbacks": [
        {"point": "user_prompt_submit", "policy_id": "closed", "policy_version": 1},
    ]});
    let agent = rpc
        .open(hosted(vec![text_turn("Done.")], "default", extra))
        .await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let error = rpc
        .until_end(&run_id)
        .await
        .error
        .expect("the prompt is blocked");
    assert_eq!(error.code, code::PROMPT_BLOCKED);
    assert_eq!(error.data.unwrap()["reason"], "no prompts today");
}
