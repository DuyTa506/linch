//! `server.info`: the handshake a client makes at connect. Every caller gets
//! the same answer, a local client and a tenant alike, and it holds only
//! facts of the build, never a tenant's own caps, names, hosts or paths.

mod common;

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, MAX_LINE_BYTES};
use serde_json::{Value, json};

/// The whole answer of this build. A change to it is a change of the wire:
/// bump `protocol::PROTOCOL_VERSION` only when an older client would
/// misread the new form.
fn expected() -> Value {
    let mut backends = vec!["local"];
    if cfg!(target_os = "linux") {
        backends.push("sandbox");
    }
    backends.extend(["docker", "remote"]);
    json!({
        "server_version": env!("CARGO_PKG_VERSION"),
        "protocol": 1,
        "contract_version": 1,
        "capabilities": [
            "cache_isolation",
            "provider_info",
            "run_steer",
            "schedule",
            "workflow",
        ],
        "backends": backends,
        "limits": {
            "max_message_bytes": MAX_LINE_BYTES,
            "default_max_runs": 16,
        },
    })
}

#[tokio::test]
async fn a_local_client_reads_the_server_info() {
    let rpc = Rpc::new();
    let info = rpc.call(method::SERVER_INFO, json!({})).await.unwrap();
    assert_eq!(info, expected());
}

#[tokio::test]
async fn the_params_may_be_absent_or_name_the_client() {
    let rpc = Rpc::new();
    let bare = rpc.call(method::SERVER_INFO, json!({})).await.unwrap();
    let named = rpc
        .call(
            method::SERVER_INFO,
            json!({"client_version": "0.1.1", "client_protocol": 1}),
        )
        .await
        .unwrap();
    assert_eq!(bare, named);
}

#[tokio::test]
async fn unknown_params_are_refused() {
    let rpc = Rpc::new();
    let error = rpc
        .call(method::SERVER_INFO, json!({"verbose": true}))
        .await
        .unwrap_err();
    assert_eq!(code_of(error), linch_rpc::code::INVALID_PARAMS);
}

#[tokio::test]
async fn a_client_version_that_is_not_short_text_is_refused() {
    let rpc = Rpc::new();
    for version in ["x".repeat(65), "1.0\nINFO forged".to_owned()] {
        let error = rpc
            .call(method::SERVER_INFO, json!({"client_version": version}))
            .await
            .unwrap_err();
        assert_eq!(code_of(error), linch_rpc::code::INVALID_PARAMS);
    }
}

#[tokio::test]
async fn a_tenant_gets_the_answer_of_a_local_client() {
    // A tenant with its own caps, allowlists and name: none shows.
    let allow = Allowlists::default()
        .max_runs(3)
        .max_agents(2)
        .scripted_provider(true);
    let rpc = Rpc::remote(tenant("acme-private", allow));
    let info = rpc.call(method::SERVER_INFO, json!({})).await.unwrap();
    assert_eq!(info, expected());
    let text = info.to_string();
    for secret in ["acme-private", "postgres", "://", "/home", "/tmp"] {
        assert!(!text.contains(secret), "the answer holds {secret:?}");
    }
}

#[tokio::test]
async fn the_answer_holds_no_unlisted_member() {
    // The shape is closed on purpose: a new member is a reviewed change.
    let rpc = Rpc::new();
    let info = rpc.call(method::SERVER_INFO, json!({})).await.unwrap();
    let mut names: Vec<_> = info.as_object().unwrap().keys().cloned().collect();
    names.sort();
    assert_eq!(
        names,
        [
            "backends",
            "capabilities",
            "contract_version",
            "limits",
            "protocol",
            "server_version"
        ]
    );
}
