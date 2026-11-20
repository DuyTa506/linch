//! The server log of a policy problem hidden from a tenant. Alone in its
//! file: the subscriber is this process's, since a callsite first met by a
//! thread without one stays off for the whole process, which other tests in
//! one binary would trigger.

mod common;

use std::sync::{Arc, Mutex};

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, ClientError};
use serde_json::{Value, json};
use tracing::field::{Field, Visit};

/// Keeps the fields of every WARN event of `linch_rpc`.
struct Warns(Arc<Mutex<Vec<String>>>);

struct Fields(String);

impl Visit for Fields {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={value:?} ", field.name()));
    }
}

impl tracing::Subscriber for Warns {
    fn enabled(&self, metadata: &tracing::Metadata<'_>) -> bool {
        *metadata.level() == tracing::Level::WARN && metadata.target().starts_with("linch_rpc")
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut fields = Fields(String::new());
        event.record(&mut fields);
        self.0.lock().unwrap().push(fields.0);
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

fn config(policy: Option<Value>, tools: Value) -> Value {
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    config["tools"] = tools;
    if let Some(policy) = policy {
        config["spec"]["policy"] = policy;
    }
    config
}

fn message(error: ClientError) -> String {
    match error {
        ClientError::Rpc(object) => object.message,
        other => panic!("{other:?}"),
    }
}

/// An open that fails on what the operator's required policy names is
/// answered with one sentence and logged in full with the tenant's name; an
/// open that fails on the tenant's own rule is answered with it and not
/// logged.
#[tokio::test]
async fn a_hidden_policy_error_is_logged_with_its_tenant() {
    let lines = Arc::new(Mutex::new(Vec::new()));
    tracing::subscriber::set_global_default(Warns(Arc::clone(&lines))).unwrap();

    let required: linch::ReliabilityPolicy = serde_json::from_value(json!({
        "prerequisites": [{"tool": "update", "requires": ["lookup"]}],
    }))
    .unwrap();
    let allow = Allowlists::new()
        .required_policy(Some(required))
        .scripted_provider(true);
    let remote = Rpc::remote(tenant("acme", allow));

    let own = json!({"gates": [{"name": "mine"}]});
    let tools = json!([host_tool("lookup", "read"), host_tool("update", "read")]);
    let shown = (remote
        .call(method::AGENT_OPEN, config(Some(own), tools))
        .await)
        .unwrap_err();
    assert!(message(shown).contains("mine"));
    assert!(
        lines.lock().unwrap().is_empty(),
        "a message that is shown is not logged: {:?}",
        lines.lock().unwrap()
    );

    let toolless = config(None, json!([]));
    let hidden = (remote.call(method::AGENT_OPEN, toolless).await).unwrap_err();
    let hidden = message(hidden);
    assert!(!hidden.contains("update") && !hidden.contains("lookup"));
    let lines = lines.lock().unwrap();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].contains("acme") && lines[0].contains("update"),
        "{lines:?}"
    );
}
