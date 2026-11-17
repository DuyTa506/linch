//! `spec.tool_retry` over RPC: a host tool declared `idempotent` that fails
//! with the class the policy lists runs again and each retry is streamed;
//! one that is not, never does; and a tenant's policy is bounded.

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use common::*;
use linch::Event;
use linch_rpc::{Allowlists, ErrorObject, protocol::method};
use serde_json::{Value, json};

/// A client whose host tool fails its first `fail_times` calls with the
/// class `transient` (or none) and then answers.
fn client(fail_times: u32, class: Option<&'static str>) -> (Rpc, Arc<AtomicU32>) {
    let calls = Arc::new(AtomicU32::new(0));
    let counted = Arc::clone(&calls);
    let rpc = Rpc::with_handler(handler(move |method, _params, _cancel| {
        let counted = Arc::clone(&counted);
        async move {
            assert_eq!(method, method::TOOL_CALL);
            if counted.fetch_add(1, Ordering::SeqCst) < fail_times {
                return Err(ErrorObject {
                    code: -32000,
                    message: "busy".into(),
                    data: class.map(|class| json!({"code": class})),
                });
            }
            Ok(json!({"value": "found"}))
        }
    }));
    (rpc, calls)
}

fn config(idempotent: bool, retry: Value) -> Value {
    let mut tool = host_tool("lookup", "read");
    tool["idempotent"] = json!(idempotent);
    let turns = vec![
        tool_turn("call_1", "lookup", json!({"q": "cats"})),
        text_turn("OK."),
    ];
    hosted(
        turns,
        "default",
        json!({"tools": [tool], "spec": {"model": MODEL, "max_turns": 100, "tool_retry": retry}}),
    )
}

async fn run(rpc: &mut Rpc, config: Value) -> Ended {
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    ended
}

fn retries(ended: &Ended) -> Vec<Value> {
    (ended.envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ToolRetry(retry) => Some(serde_json::to_value(retry).unwrap()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn an_idempotent_host_tool_that_reports_a_transient_error_is_retried() {
    let (mut rpc, calls) = client(2, Some("transient"));
    let retry = json!({"base_delay_ms": 1, "max_delay_ms": 2});
    let ended = run(&mut rpc, config(true, retry)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    let retried = retries(&ended);
    assert_eq!(retried.len(), 2);
    assert_eq!(retried[0]["attempt"], 2);
    assert_eq!(retried[0]["error_class"], "transient");
    assert_eq!(retried[1]["max_attempts"], 3);
    assert!(tool_results(&ended)[0].to_string().contains("found"));
}

#[tokio::test]
async fn a_host_tool_that_is_not_declared_idempotent_runs_once() {
    let (mut rpc, calls) = client(2, Some("transient"));
    let retry = json!({"base_delay_ms": 1, "max_delay_ms": 2, "tools": ["lookup"]});
    let ended = run(&mut rpc, config(false, retry)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(retries(&ended).is_empty());
    assert_eq!(tool_results(&ended)[0]["is_error"], true);
}

#[tokio::test]
async fn an_error_with_no_class_or_another_one_is_not_retried() {
    for class in [None, Some("fatal")] {
        let (mut rpc, calls) = client(2, class);
        let retry = json!({"base_delay_ms": 1, "max_delay_ms": 2});
        let ended = run(&mut rpc, config(true, retry)).await;
        assert_eq!(calls.load(Ordering::SeqCst), 1, "{class:?}");
        assert!(retries(&ended).is_empty());
    }
}

#[tokio::test]
async fn a_policy_that_cannot_run_is_refused_at_open() {
    let rpc = Rpc::new();
    let error = rpc
        .call(method::AGENT_OPEN, config(true, json!({"max_attempts": 1})))
        .await
        .unwrap_err();
    assert_eq!(code_of(error), linch_rpc::code::BUILD_INVALID);
}

/// A retry runs the tool again and writes an event on the server, so a
/// tenant may ask for few attempts and short waits; a local connection may
/// ask for more.
#[tokio::test]
async fn a_tenant_may_not_raise_the_retry_bounds() {
    let remote = Rpc::remote(tenant("acme", Allowlists::new().scripted_provider(true)));
    let open = |retry: Value| {
        let mut config = config(true, retry);
        config["namespace"] = json!("acme");
        // A tenant's project root must be one of its workspace roots.
        config.as_object_mut().unwrap().remove("permissions");
        config
    };
    for within in [
        json!({}),
        json!({"max_attempts": 5, "max_delay_ms": 60_000}),
    ] {
        assert_eq!(
            refused(&remote, open(within.clone())).await,
            None,
            "{within}"
        );
    }
    for beyond in [
        json!({"max_attempts": 6}),
        json!({"max_delay_ms": 60_001}),
        json!({"retry_on": (0..33).map(|n| format!("c{n}")).collect::<Vec<_>>()}),
    ] {
        let kind = refused(&remote, open(beyond.clone())).await;
        assert_eq!(kind.as_deref(), Some("tool_retry"), "{beyond}");
    }
    let local = Rpc::new();
    let big = json!({"max_attempts": 50, "max_delay_ms": 600_000});
    assert!(
        local
            .call(method::AGENT_OPEN, config(true, big))
            .await
            .is_ok()
    );
}
