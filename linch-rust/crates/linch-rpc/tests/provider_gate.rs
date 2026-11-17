//! A tenant's provider concurrency over RPC: `agent.open` takes
//! `max_provider_concurrency`, a tenant may lower the operator's cap but
//! not raise it, and a local connection has no cap.

mod common;

use std::num::NonZeroU32;

use common::*;
use linch_rpc::Allowlists;
use serde_json::{Value, json};

fn config(limit: Value) -> Value {
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    if !limit.is_null() {
        config["max_provider_concurrency"] = limit;
    }
    config
}

#[tokio::test]
async fn a_tenant_may_not_raise_the_operators_provider_concurrency() {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .max_provider_concurrency(NonZeroU32::new(3));
    let remote = Rpc::remote(tenant("acme", allow));
    for within in [Value::Null, json!(3), json!(1)] {
        assert_eq!(refused(&remote, config(within.clone())).await, None);
    }
    assert_eq!(
        refused(&remote, config(json!(4))).await.as_deref(),
        Some("provider_concurrency")
    );
    // A cap of zero never decodes.
    assert!(remote.call("agent.open", config(json!(0))).await.is_err());
}

#[tokio::test]
async fn a_local_connection_has_no_cap() {
    let local = Rpc::new();
    let mut open = config(json!(1000));
    open["namespace"] = json!("rpc");
    local.open(open).await;
}
