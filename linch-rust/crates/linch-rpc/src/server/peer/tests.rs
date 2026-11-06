use std::time::Instant;

use serde_json::json;
use tokio::sync::mpsc;

use super::*;

/// A peer whose output lines land in the returned receiver.
fn peer() -> (Peer, mpsc::Receiver<String>) {
    let (sender, lines) = mpsc::channel(16);
    (Peer::new(Outbox(sender)), lines)
}

async fn next(lines: &mut mpsc::Receiver<String>) -> JsonValue {
    serde_json::from_str(&lines.recv().await.expect("a line")).unwrap()
}

fn ok(id: &JsonValue, result: JsonValue) -> Response {
    let id = Id::String(id.as_str().unwrap().to_owned());
    Response::ok(id, result)
}

#[tokio::test]
async fn a_call_gets_its_answer_under_a_server_id() {
    let (peer, mut lines) = peer();
    let caller = peer.clone();
    let call = tokio::spawn(async move {
        let params = json!({"q": 1}).as_object().unwrap().clone();
        caller.call("tool.call", params, None, None).await
    });
    let first = next(&mut lines).await;
    assert_eq!(first["method"], "tool.call");
    assert_eq!(first["id"], "srv-1");
    assert_eq!(first["params"], json!({"q": 1}));
    // A numeric id never matches a server request.
    peer.answer(Response::ok(Id::Number(1.into()), json!({"wrong": true})));
    peer.answer(ok(&first["id"], json!({"value": 7})));
    assert_eq!(call.await.unwrap().unwrap(), json!({"value": 7}));

    let caller = peer.clone();
    let failing = tokio::spawn(async move {
        caller
            .call("tool.call", JsonObject::new(), None, None)
            .await
    });
    let second = next(&mut lines).await;
    assert_eq!(second["id"], "srv-2");
    let error = ErrorObject::new(5, "nope");
    peer.answer(Response::err(Id::String("srv-2".into()), error.clone()));
    assert!(matches!(failing.await.unwrap(), Err(CallError::Rpc(e)) if e == error));
    assert!(
        lines.try_recv().is_err(),
        "an answered call sends no cancel"
    );
}

#[tokio::test]
async fn closing_fails_waiting_calls_at_once_and_refuses_new_ones() {
    let (peer, mut lines) = peer();
    let caller = peer.clone();
    let started = Instant::now();
    let call = tokio::spawn(async move {
        let limit = Some(Duration::from_secs(60));
        caller
            .call("hook.call", JsonObject::new(), limit, None)
            .await
    });
    next(&mut lines).await;
    peer.close();
    assert!(matches!(call.await.unwrap(), Err(CallError::Closed)));
    assert!(started.elapsed() < Duration::from_secs(5));
    let later = peer.call("hook.call", JsonObject::new(), None, None).await;
    assert!(matches!(later, Err(CallError::Closed)));
    assert!(lines.try_recv().is_err(), "nothing more was sent");
}

#[tokio::test]
async fn a_timeout_sends_cancel_and_a_late_answer_is_dropped() {
    let (peer, mut lines) = peer();
    let limit = Some(Duration::from_millis(20));
    let result = peer.call("tool.call", JsonObject::new(), limit, None).await;
    assert!(matches!(result, Err(CallError::Timeout(d)) if d == Duration::from_millis(20)));
    let request = next(&mut lines).await;
    let cancel = next(&mut lines).await;
    assert_eq!(
        cancel,
        json!({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": request["id"]}})
    );
    // The late answer finds no waiter; nothing breaks.
    peer.answer(ok(&request["id"], json!({})));
    assert!(lock(&peer.0.calls).waiting.is_empty());
}

#[tokio::test]
async fn a_cancel_token_or_a_dropped_call_sends_cancel() {
    let (peer, mut lines) = peer();
    let token = CancellationToken::new();
    let caller = peer.clone();
    let watched = token.clone();
    let call = tokio::spawn(async move {
        let params = JsonObject::new();
        caller.call("tool.call", params, None, Some(&watched)).await
    });
    let request = next(&mut lines).await;
    token.cancel();
    assert!(matches!(call.await.unwrap(), Err(CallError::Cancelled)));
    let cancel = next(&mut lines).await;
    assert_eq!(cancel["params"]["id"], request["id"]);

    let caller = peer.clone();
    let call = tokio::spawn(async move {
        caller
            .call("hook.call", JsonObject::new(), None, None)
            .await
    });
    let request = next(&mut lines).await;
    call.abort();
    let _ = call.await;
    let cancel = next(&mut lines).await;
    assert_eq!(cancel["method"], "$/cancelRequest");
    assert_eq!(cancel["params"]["id"], request["id"]);
}
