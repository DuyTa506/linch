//! The login of a connection that requires one: the first message must be
//! `connection.auth` with a known token, and anything else closes the
//! connection without echoing the token.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::*;
use linch::PgPools;
use linch_rpc::protocol::{Authenticated, method};
use linch_rpc::{Access, Allowlists, ClientError, MAX_PENDING_LOGINS, Tenant, Tokens, code};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio_util::sync::CancellationToken;

const TOKEN: &str = "tok-acme-p11c-0123456789";

fn tokens() -> Arc<Tokens> {
    let acme = Arc::new(Tenant::new("acme", Allowlists::new().scripted_provider(true)).unwrap());
    let mut tokens = Tokens::new();
    tokens.insert(TOKEN, acme).unwrap();
    Arc::new(tokens)
}

fn login() -> Access {
    Access::Login(tokens())
}

#[tokio::test]
async fn a_valid_token_names_the_tenant_and_opens_the_connection() {
    let mut rpc = Rpc::remote(login());
    let auth = json!({"token": TOKEN});
    let who: Authenticated = rpc
        .client
        .call(method::CONNECTION_AUTH, &auth)
        .await
        .unwrap();
    assert_eq!(who.tenant, "acme");
    let mut config = scripted(vec![text_turn("hello")], None, "");
    config["namespace"] = json!("acme");
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "hi").await;
    assert!(rpc.until_end(&run_id).await.error.is_none());
    // A second login is not a thing.
    let again = rpc.call(method::CONNECTION_AUTH, auth).await.unwrap_err();
    assert_eq!(code_of(again), code::INVALID_REQUEST);
}

/// Send `first` as the first line, then a valid call; return every line
/// the server wrote before closing.
async fn refused_lines(first: &str) -> Vec<Value> {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let stop = CancellationToken::new();
    let serving = tokio::spawn(linch_rpc::serve_lines(
        server_reader,
        server_writer,
        login(),
        Arc::new(PgPools::new()),
        stop,
    ));
    let (reader, mut writer) = tokio::io::split(ours);
    let cancel = json!({"jsonrpc": "2.0", "id": 2, "method": "run.cancel",
                        "params": {"run_id": "r"}});
    writer
        .write_all(format!("{first}\n{cancel}\n").as_bytes())
        .await
        .unwrap();
    let ended = tokio::time::timeout(Duration::from_secs(5), serving).await;
    ended
        .expect("the server closes the connection")
        .unwrap()
        .unwrap();
    let mut lines = BufReader::new(reader).lines();
    let mut seen = Vec::new();
    while let Some(line) = lines.next_line().await.unwrap() {
        assert!(
            !line.contains("wrong-token-value"),
            "the token came back: {line}"
        );
        seen.push(serde_json::from_str(&line).unwrap());
    }
    seen
}

#[tokio::test]
async fn anything_but_a_valid_login_is_refused_and_closes_the_connection() {
    let wrong = json!({"jsonrpc": "2.0", "id": 1, "method": "connection.auth",
                       "params": {"token": "wrong-token-value"}});
    let other = json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open",
                       "params": {"token": TOKEN}});
    let extra = json!({"jsonrpc": "2.0", "id": 1, "method": "connection.auth",
                       "params": {"token": TOKEN, "tenant": "beta"}});
    for first in [
        wrong.to_string(),
        other.to_string(),
        extra.to_string(),
        "{".into(),
    ] {
        let lines = refused_lines(&first).await;
        assert_eq!(lines.len(), 1, "{first}: {lines:?}");
        assert_eq!(lines[0]["error"]["code"], code::UNAUTHENTICATED, "{first}");
    }
}

#[tokio::test]
async fn a_closed_login_connection_fails_later_calls() {
    let rpc = Rpc::remote(login());
    let wrong = json!({"token": "wrong-token-value"});
    let error = rpc.call(method::CONNECTION_AUTH, wrong).await.unwrap_err();
    assert_eq!(code_of(error), code::UNAUTHENTICATED);
    let after = rpc.call(method::RUN_CANCEL, json!({"run_id": "r"}));
    let after = tokio::time::timeout(Duration::from_secs(5), after).await;
    assert!(matches!(after.expect("no hang"), Err(ClientError::Closed)));
}

#[tokio::test]
async fn stop_ends_a_connection_that_waits_for_its_login() {
    let (_ours, theirs) = tokio::io::duplex(1024);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let stop = CancellationToken::new();
    let serving = tokio::spawn(linch_rpc::serve_lines(
        server_reader,
        server_writer,
        login(),
        Arc::new(PgPools::new()),
        stop.clone(),
    ));
    stop.cancel();
    let ended = tokio::time::timeout(Duration::from_secs(5), serving).await;
    ended.expect("stop ends the login wait").unwrap().unwrap();
}

#[tokio::test]
async fn past_max_pending_logins_a_new_connection_is_refused_at_once() {
    let tokens = tokens();
    let stop = CancellationToken::new();
    let connect = || {
        let (ours, theirs) = tokio::io::duplex(64 * 1024);
        let (reader, writer) = tokio::io::split(theirs);
        let pools = Arc::new(PgPools::new());
        let access = Access::Login(Arc::clone(&tokens));
        let serve = linch_rpc::serve_lines(reader, writer, access, pools, stop.clone());
        (tokio::io::split(ours), tokio::spawn(serve))
    };
    let mut waiting: Vec<_> = (0..MAX_PENDING_LOGINS).map(|_| connect()).collect();
    // Let every one of them reach its login.
    tokio::time::sleep(Duration::from_millis(100)).await;
    let first_line = |reader| async move {
        let mut lines = BufReader::new(reader).lines();
        let line = tokio::time::timeout(Duration::from_secs(5), lines.next_line()).await;
        let line = line.expect("an answer well before the login timeout");
        serde_json::from_str::<Value>(&line.unwrap().unwrap()).unwrap()
    };
    let ((reader, _writer), serving) = connect();
    let refused = first_line(reader).await;
    assert_eq!(refused["error"]["code"], code::LIMIT_REACHED);
    let expected = json!({"kind": "logins", "max": MAX_PENDING_LOGINS});
    assert_eq!(refused["error"]["data"], expected);
    serving.await.unwrap().unwrap();
    // A login that completes frees its place for a new connection.
    let ((reader, mut writer), _serving) = waiting.pop().unwrap();
    let auth = json!({"jsonrpc": "2.0", "id": 1, "method": "connection.auth",
                      "params": {"token": TOKEN}});
    writer
        .write_all(format!("{auth}\n").as_bytes())
        .await
        .unwrap();
    assert_eq!(first_line(reader).await["result"]["tenant"], "acme");
    let ((reader, mut writer), _serving) = connect();
    writer
        .write_all(format!("{auth}\n").as_bytes())
        .await
        .unwrap();
    assert_eq!(first_line(reader).await["result"]["tenant"], "acme");
    stop.cancel();
}
