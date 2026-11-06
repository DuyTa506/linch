//! Tenant scoping on a remote connection: an agent's namespace must be the
//! tenant's, a principal may not name another tenant, and another tenant's
//! handles and runs are unknown. On one shared Postgres database a tenant
//! still reaches only its own runs.

mod common;

use std::future::pending;

use common::pg::TestDb;
use common::*;
use linch::events::kinds::ResultSubtype;
use linch::{Event, RunId};
use linch_rpc::protocol::{Cancelled, Events, method};
use linch_rpc::{Access, Allowlists, ErrorObject, code};
use serde_json::{Value, json};
use tokio::sync::mpsc;

/// What these tests' tenants may use: the scripted provider.
fn allow() -> Allowlists {
    Allowlists::new().scripted_provider(true)
}

fn acme() -> Access {
    tenant("acme", allow())
}

/// A scripted config in `namespace`.
fn config_in(namespace: &str, turns: Vec<linch::ScriptedTurn>) -> Value {
    let mut config = scripted(turns, None, "");
    config["namespace"] = json!(namespace);
    config
}

#[tokio::test]
async fn a_tenant_opens_agents_only_in_its_own_namespaces() {
    let rpc = Rpc::remote(acme());
    for own in ["acme", "acme/support", "acme/eu/support"] {
        rpc.open(config_in(own, vec![])).await;
    }
    for other in ["beta", "acme-eu", "acmesupport", "Acme", "beta/acme", ""] {
        let error = rpc
            .call(method::AGENT_OPEN, config_in(other, vec![]))
            .await
            .unwrap_err();
        let expected = (code::NOT_ALLOWED, Some("namespace".to_owned()));
        assert_eq!(refusal(error), expected, "{other:?}");
    }
}

#[tokio::test]
async fn a_principal_must_name_the_connections_tenant() {
    let mut rpc = Rpc::remote(acme());
    let agent = rpc.open(config_in("acme", vec![text_turn("hi")])).await;
    let foreign = json!({"tenant": "beta", "user": "u1"});
    let calls = [
        (
            method::RUN_START,
            json!({"session_id": "s1", "prompt": "p"}),
        ),
        (method::RUN_RESUME, json!({"run_id": "r1"})),
        (
            method::RUN_APPROVE,
            json!({"run_id": "r1", "decisions": {}}),
        ),
    ];
    for (name, mut params) in calls {
        params["agent"] = json!(agent);
        params["principal"] = foreign.clone();
        let error = rpc.call(name, params).await.unwrap_err();
        let expected = (code::NOT_ALLOWED, Some("principal".to_owned()));
        assert_eq!(refusal(error), expected, "{name}");
    }
    let own = json!({"agent": agent, "session_id": "s1", "prompt": "p",
                     "principal": {"tenant": "acme", "user": "u1"}});
    let run_id = rpc.accepted(method::RUN_START, own).await;
    assert!(rpc.until_end(&run_id).await.error.is_none());
}

#[tokio::test]
async fn another_tenants_handles_and_runs_are_unknown() {
    let (arrived, mut calls) = mpsc::unbounded_channel();
    let mut owner = Rpc::remote_with(
        acme(),
        handler(move |_, _, _| {
            let _ = arrived.send(());
            pending::<Result<Value, ErrorObject>>()
        }),
    );
    let mut config = hosted(
        vec![
            tool_turn("call_1", "lookup", json!({})),
            text_turn("unused"),
        ],
        "default",
        json!({"tools": [host_tool("lookup", "read")]}),
    );
    config["namespace"] = json!("acme");
    let agent = owner.open(config).await;
    let run_id = owner.start(&agent, "s1", "Look it up.").await;
    calls.recv().await.expect("the run waits in its tool");

    let other = Rpc::remote(tenant("beta", allow()));
    let by_handle = [
        (
            method::RUN_START,
            json!({"agent": agent, "session_id": "s", "prompt": "p"}),
        ),
        (
            method::RUN_EVENTS,
            json!({"agent": agent, "run_id": run_id}),
        ),
        (
            method::RUN_RESUME,
            json!({"agent": agent, "run_id": run_id}),
        ),
        (method::AGENT_CLOSE, json!({"agent": agent})),
    ];
    for (name, params) in by_handle {
        let error = other.call(name, params).await.unwrap_err();
        assert_eq!(code_of(error), code::AGENT_NOT_FOUND, "{name}");
    }
    let cancel = json!({"run_id": run_id});
    let cancelled: Cancelled = other
        .client
        .call(method::RUN_CANCEL, &cancel)
        .await
        .unwrap();
    assert!(!cancelled.cancelled);
    let own = other.open(config_in("beta", vec![])).await;
    let events = json!({"agent": own, "run_id": run_id});
    let error = other.call(method::RUN_EVENTS, events).await.unwrap_err();
    assert_eq!(code_of(error), code::NOT_FOUND);

    // The owner's run was untouched until the owner cancels it.
    let cancelled: Cancelled = owner
        .client
        .call(method::RUN_CANCEL, &cancel)
        .await
        .unwrap();
    assert!(cancelled.cancelled);
    match owner.until_end(&run_id).await.last() {
        Event::Result(result) => assert_eq!(result.subtype, ResultSubtype::Aborted),
        other => panic!("expected a result, got {other:?}"),
    }
}

#[tokio::test]
async fn on_shared_postgres_a_tenant_reaches_only_its_own_runs() {
    let Some(db) = TestDb::empty("tenants").await else {
        return;
    };
    let on_db = |namespace: &str| {
        let mut config = config_in(namespace, vec![text_turn("stored")]);
        config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
        config
    };
    let on_shared = || allow().postgres_dsns(std::slice::from_ref(&db.dsn)).unwrap();
    let mut owner = Rpc::remote(tenant("acme", on_shared()));
    let agent = owner.open(on_db("acme")).await;
    let run_id = owner.start(&agent, "s1", "go").await;
    let durable = owner.until_end(&run_id).await;
    assert!(durable.error.is_none(), "{:?}", durable.error);

    // Another connection of the same tenant reads the run.
    let colleague = Rpc::remote(tenant("acme", on_shared()));
    let theirs = colleague.open(on_db("acme")).await;
    let params = json!({"agent": theirs, "run_id": run_id});
    let events: Events = colleague
        .client
        .call(method::RUN_EVENTS, &params)
        .await
        .unwrap();
    assert!(!events.events.is_empty());

    // Another tenant cannot open that namespace, and in its own the run
    // does not exist.
    let mut stranger = Rpc::remote(tenant("beta", on_shared()));
    let error = stranger
        .call(method::AGENT_OPEN, on_db("acme"))
        .await
        .unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("namespace".into()))
    );
    let own = stranger.open(on_db("beta")).await;
    let params = json!({"agent": own, "run_id": run_id});
    let error = stranger
        .call(method::RUN_EVENTS, params.clone())
        .await
        .unwrap_err();
    assert_eq!(code_of(error), code::NOT_FOUND);
    let resumed: RunId = stranger.accepted(method::RUN_RESUME, params).await;
    let ended = stranger.until_end(&resumed).await;
    assert_eq!(ended.error.map(|e| e.code), Some(code::NOT_FOUND));
    db.drop().await;
}
