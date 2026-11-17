//! `spec.tool_cache` over RPC: a repeated call of a named host tool is
//! served from the cache and its `tool_call_end` says so, and a tenant may
//! use the cache only when its allowlists say, within bounds.

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use common::*;
use linch::Event;
use linch_rpc::Allowlists;
use linch_rpc::protocol::method;
use serde_json::{Value, json};

fn config(cache: Value) -> Value {
    let call = |id: &str| tool_turn(id, "lookup", json!({"q": "cats"}));
    let turns = vec![call("call_1"), call("call_2"), text_turn("OK.")];
    hosted(
        turns,
        "default",
        json!({"tools": [host_tool("lookup", "read")],
               "spec": {"model": MODEL, "tool_cache": cache}}),
    )
}

#[tokio::test]
async fn a_repeated_host_tool_call_is_served_from_the_cache() {
    let calls = Arc::new(AtomicU32::new(0));
    let counted = Arc::clone(&calls);
    let mut rpc = Rpc::with_handler(handler(move |_, _, _| {
        counted.fetch_add(1, Ordering::SeqCst);
        async { Ok(json!({"value": "found"})) }
    }));
    let agent = rpc.open(config(json!({"tools": ["lookup"]}))).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "the second call never reached the client"
    );
    let hits: Vec<bool> = (ended.envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ToolCallEnd(end) => Some(end.cache_hit),
            _ => None,
        })
        .collect();
    assert_eq!(hits, [false, true]);
    let results = tool_results(&ended);
    assert!(
        results.iter().all(|r| r.to_string().contains("found")),
        "{results:?}"
    );
}

#[tokio::test]
async fn a_cache_that_cannot_run_is_refused_at_open() {
    let rpc = Rpc::new();
    for cache in [
        json!({"tools": []}),
        json!({"tools": ["lookup"], "max_entries": 0}),
    ] {
        let error = rpc
            .call(method::AGENT_OPEN, config(cache))
            .await
            .unwrap_err();
        assert_eq!(code_of(error), linch_rpc::code::BUILD_INVALID);
    }
    let unnamed = rpc.call(method::AGENT_OPEN, config(json!({}))).await;
    assert_ne!(
        code_of(unnamed.unwrap_err()),
        linch_rpc::code::BUILD_INVALID,
        "a decode error"
    );
}

/// The cache holds results in the server, shared by a tenant's sessions, so
/// a tenant needs the server's say and stays within its bounds; a local
/// connection needs neither.
#[tokio::test]
async fn a_tenant_uses_the_cache_only_when_allowed_and_within_bounds() {
    let open = |cache: Value| {
        let mut config = config(cache);
        config["namespace"] = json!("acme");
        // A tenant's project root must be one of its workspace roots.
        config.as_object_mut().unwrap().remove("permissions");
        config
    };
    let within = json!({"tools": ["lookup"], "max_entries": 1024, "ttl_ms": 3_600_000,
                        "max_value_bytes": 1_048_576});
    let closed = Rpc::remote(tenant("acme", Allowlists::new().scripted_provider(true)));
    let kind = refused(&closed, open(within.clone())).await;
    assert_eq!(kind.as_deref(), Some("tool_cache"));

    let allowed = Allowlists::new().scripted_provider(true).tool_cache(true);
    let remote = Rpc::remote(tenant("acme", allowed));
    assert_eq!(refused(&remote, open(within)).await, None);
    for beyond in [
        json!({"tools": ["lookup"], "max_entries": 1025}),
        json!({"tools": ["lookup"], "ttl_ms": 3_600_001}),
        json!({"tools": ["lookup"], "max_value_bytes": 1_048_577}),
    ] {
        let kind = refused(&remote, open(beyond.clone())).await;
        assert_eq!(kind.as_deref(), Some("tool_cache"), "{beyond}");
    }
    let local = Rpc::new();
    let big = json!({"tools": ["lookup"], "max_entries": 100_000});
    assert!(local.call(method::AGENT_OPEN, config(big)).await.is_ok());
}
