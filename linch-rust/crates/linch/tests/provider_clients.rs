//! One HTTP client per set of provider settings and per `PgPools`: agents
//! opened with one `PgPools` whose providers have the same timeouts and
//! default headers share a client; another `PgPools` (another tenant) never
//! does, and a client goes with the last agent that holds it.

use std::sync::Arc;

use linch::{AgentConfig, Hooks, OpenedAgent, PgPools, ToolRegistry};
use serde_json::{Value, json};

fn config(headers: Value) -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "clients",
        "spec": {"model": "m"},
        // A window is set, so opening probes nothing: no request is made.
        "provider": {
            "id": "vllm",
            "base_url": "http://127.0.0.1:9/v1",
            "context_window": 8000,
            "request": {"default_headers": headers},
        },
    }))
    .unwrap()
}

async fn open(pools: &PgPools, headers: Value) -> OpenedAgent {
    let (tools, hooks) = (Arc::new(ToolRegistry::new()), Arc::new(Hooks::default()));
    config(headers)
        .open_with(tools, hooks, pools)
        .await
        .unwrap()
}

#[tokio::test]
async fn fifty_opens_of_one_provider_build_one_client() {
    let pools = PgPools::new();
    let mut agents = Vec::new();
    for _ in 0..50 {
        agents.push(open(&pools, json!({})).await);
    }
    assert_eq!(pools.http_clients(), 1);
    agents.clear();
    assert_eq!(pools.http_clients(), 0, "no agent holds it any more");
}

#[tokio::test]
async fn two_tenants_never_share_a_client() {
    let (a, b) = (PgPools::new(), PgPools::new());
    let _first = open(&a, json!({})).await;
    let _second = open(&b, json!({})).await;
    assert_eq!((a.http_clients(), b.http_clients()), (1, 1));
}

#[tokio::test]
async fn other_default_headers_are_another_client() {
    let pools = PgPools::new();
    let _plain = open(&pools, json!({})).await;
    let _one = open(&pools, json!({"x-team": "a"})).await;
    let _same = open(&pools, json!({"x-team": "a"})).await;
    let _other = open(&pools, json!({"x-team": "b"})).await;
    assert_eq!(pools.http_clients(), 3);
}
