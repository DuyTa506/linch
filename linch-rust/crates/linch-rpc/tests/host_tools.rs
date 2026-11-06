//! Host tools: declared in `agent.open`, run by the client through
//! `tool.call`, with errors, cancel, timeout, the required scope, and only
//! read-scope calls overlapping.

mod common;

use std::collections::HashMap;
use std::future::pending;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use linch::events::kinds::ResultSubtype;
use linch::provider::scripted::TurnBuilder;
use linch::{Event, JsonValue};
use linch_rpc::protocol::{ToolCallParams, method};
use linch_rpc::{ClientError, ErrorObject, code};
use serde_json::{Value, json};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

fn subtype(ended: &Ended) -> ResultSubtype {
    match ended.last() {
        Event::Result(result) => result.subtype,
        other => panic!("expected a result, got {other:?}"),
    }
}

fn call_of(params: linch::JsonObject) -> ToolCallParams {
    serde_json::from_value(Value::Object(params)).unwrap()
}

/// A client whose host tool calls hang; each call's cancel token is sent
/// to the returned receiver as the call arrives.
fn hanging() -> (Rpc, mpsc::UnboundedReceiver<CancellationToken>) {
    let (tokens, arrived) = mpsc::unbounded_channel();
    let rpc = Rpc::with_handler(handler(move |_, _, cancel| {
        let _ = tokens.send(cancel);
        pending::<Result<Value, ErrorObject>>()
    }));
    (rpc, arrived)
}

#[tokio::test]
async fn a_host_tool_runs_in_the_client() {
    let (seen, mut calls) = mpsc::unbounded_channel();
    let mut rpc = Rpc::with_handler(handler(move |method, params, _| {
        let seen = seen.clone();
        async move {
            assert_eq!(method, method::TOOL_CALL);
            let call = call_of(params);
            let found = format!("found {}", call.input["q"].as_str().unwrap());
            seen.send(call).unwrap();
            Ok(json!({"value": found, "metadata": {"hits": 1}}))
        }
    }));
    let turns = vec![
        tool_turn("call_1", "lookup", json!({"q": "cats"})),
        text_turn("Done."),
    ];
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Find cats").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let Event::Init(init) = &ended.envelopes[0].event else {
        panic!("first event {:?}", ended.envelopes[0].event);
    };
    assert_eq!(init.tools, ["lookup"]);
    let results = tool_results(&ended);
    assert!(results[0].to_string().contains("found cats"), "{results:?}");
    assert_eq!(subtype(&ended), ResultSubtype::Success);

    let call = calls.recv().await.unwrap();
    assert_eq!(
        (call.agent.as_str(), call.name.as_str()),
        (agent.as_str(), "lookup")
    );
    assert_eq!((call.run_id, call.tool_use_id.as_str()), (run_id, "call_1"));
    assert_eq!(call.session_id.as_str(), "s1");
    assert_eq!(call.input, *json!({"q": "cats"}).as_object().unwrap());
}

#[tokio::test]
async fn a_client_error_becomes_a_tool_error_the_model_reads() {
    let mut rpc = Rpc::with_handler(handler(|_, _, _| async {
        Err(ErrorObject::new(7, "no cats here"))
    }));
    let turns = vec![
        tool_turn("call_1", "lookup", json!({})),
        text_turn("Sorry."),
    ];
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Find cats").await;
    let ended = rpc.until_end(&run_id).await;
    let results = tool_results(&ended);
    assert!(
        results[0].to_string().contains("no cats here"),
        "{results:?}"
    );
    assert_eq!(results[0]["is_error"], true, "{results:?}");
    assert_eq!(subtype(&ended), ResultSubtype::Success);
}

#[tokio::test]
async fn cancelling_a_run_cancels_its_host_call() {
    let (mut rpc, mut arrived) = hanging();
    let turns = vec![tool_turn("call_1", "wait", json!({})), text_turn("unused")];
    let tools = json!({"tools": [host_tool("wait", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Wait.").await;
    let token = arrived.recv().await.expect("the call reached the client");
    assert!(!token.is_cancelled());

    let cancel = json!({"run_id": run_id});
    rpc.call(method::RUN_CANCEL, cancel).await.unwrap();
    let ended = rpc.until_end(&run_id).await;
    assert_eq!(subtype(&ended), ResultSubtype::Aborted);
    // `$/cancelRequest` precedes the run's last envelopes on the wire.
    assert!(token.is_cancelled(), "the client was not told");
}

#[tokio::test]
async fn a_host_tool_times_out_and_the_client_is_told() {
    let (mut rpc, mut arrived) = hanging();
    let turns = vec![
        tool_turn("call_1", "wait", json!({})),
        text_turn("Gave up."),
    ];
    let mut tool = host_tool("wait", "read");
    tool["timeout_ms"] = json!(50);
    let agent = rpc
        .open(hosted(turns, "default", json!({"tools": [tool]})))
        .await;
    let run_id = rpc.start(&agent, "s1", "Wait.").await;
    let ended = rpc.until_end(&run_id).await;
    let results = tool_results(&ended);
    assert!(
        results[0].to_string().contains("timed out after 50ms"),
        "{results:?}"
    );
    assert_eq!(subtype(&ended), ResultSubtype::Success);
    let token = arrived.recv().await.unwrap();
    assert!(token.is_cancelled(), "the client was not told");
}

#[tokio::test]
async fn a_host_tool_needs_a_scope_and_a_valid_declaration() {
    let rpc = Rpc::new();
    let mut no_scope = host_tool("lookup", "read");
    no_scope.as_object_mut().unwrap().remove("scope");
    let mut zero_timeout = host_tool("lookup", "read");
    zero_timeout["timeout_ms"] = json!(0);
    let mut extra_field = host_tool("lookup", "read");
    extra_field["parallel"] = json!(true);
    let invalid = [
        json!({"tools": [no_scope]}),
        json!({"tools": [host_tool("lookup", "admin")]}),
        json!({"tools": [zero_timeout]}),
        json!({"tools": [extra_field]}),
        json!({"tools": [host_tool("bad name", "read")]}),
        json!({"tools": [host_tool("twice", "read"), host_tool("twice", "exec")]}),
        json!({"tools": [host_tool("lookup", "read")], "unknown": 1}),
    ];
    for extra in invalid {
        let text = extra.to_string();
        let error = rpc
            .call(method::AGENT_OPEN, hosted(vec![], "default", extra))
            .await
            .unwrap_err();
        let ClientError::Rpc(error) = error else {
            panic!("{text}: {error:?}");
        };
        assert_eq!(error.code, code::INVALID_PARAMS, "{text}: {error:?}");
    }

    // A host tool cannot take a workspace tool's name.
    let dir = tempfile::tempdir().unwrap();
    let mut config = scripted(vec![], Some(dir.path()), "default");
    config["tools"] = json!([host_tool("Read", "read")]);
    let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
    assert!(
        matches!(&error, ClientError::Rpc(e) if e.code == code::BUILD_INVALID),
        "{error:?}"
    );
}

#[tokio::test]
async fn only_read_scope_host_calls_overlap() {
    // Active and peak concurrent calls per tool name.
    let counts: Arc<Mutex<HashMap<String, (u32, u32)>>> = Arc::default();
    let tally = Arc::clone(&counts);
    let mut rpc = Rpc::with_handler(handler(move |_, params, _| {
        let tally = Arc::clone(&tally);
        async move {
            let name = call_of(params).name;
            {
                let mut counts = tally.lock().unwrap();
                let (active, peak) = counts.entry(name.clone()).or_default();
                *active += 1;
                *peak = (*peak).max(*active);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
            tally.lock().unwrap().get_mut(&name).unwrap().0 -= 1;
            Ok(json!({"value": JsonValue::Null}))
        }
    }));
    let pair = |tool: &str| {
        TurnBuilder::new(MODEL)
            .tool_call(&format!("{tool}_1"), tool, &json!({}))
            .tool_call(&format!("{tool}_2"), tool, &json!({}))
            .build()
    };
    let turns = vec![pair("peek"), pair("poke"), text_turn("Done.")];
    let tools = json!({"tools": [host_tool("peek", "read"), host_tool("poke", "exec")]});
    let agent = rpc.open(hosted(turns, "skip_dangerous", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert_eq!(subtype(&ended), ResultSubtype::Success);
    let counts = counts.lock().unwrap();
    assert_eq!(counts["peek"].1, 2, "read calls run together");
    assert_eq!(counts["poke"].1, 1, "exec calls run alone");
}
