//! The client against an in-process rmcp server over a duplex pipe.

mod common;

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use common::{
    Duplex, Fixture, PATIENCE, call, call_with, config, connect, eventually, standard_tools,
    text_input, tool, visible, wait_status,
};
use linch_core::{JsonObject, ToolRegistry, ToolScope};
use linch_mcp::{McpError, McpServer, ServerStatus, codes};
use serde_json::json;
use tokio_util::sync::CancellationToken;

#[tokio::test]
async fn connect_registers_each_tool_with_its_scope_and_schema() {
    let (server, _duplex, registry) = connect(Fixture::new(standard_tools())).await;
    assert_eq!(server.name(), "t");
    assert_eq!(server.status(), ServerStatus::Connected);
    assert_eq!(
        server.tools(),
        [
            "mcp__t__echo",
            "mcp__t__fail",
            "mcp__t__image",
            "mcp__t__pid",
            "mcp__t__slow"
        ]
    );
    assert!(server.skipped().is_empty());

    let snapshot = registry.snapshot();
    let echo = snapshot.resolve("mcp__t__echo").unwrap();
    assert_eq!(echo.tool().scope(), ToolScope::Read, "trusted readOnlyHint");
    assert_eq!(echo.tool().description(), "Echo text.");
    assert_eq!(
        serde_json::Value::Object(echo.tool().input_schema().clone()),
        json!({"type": "object", "properties": {"text": {"type": "string"}}})
    );
    let fail = snapshot.resolve("mcp__t__fail").unwrap();
    assert_eq!(fail.tool().scope(), ToolScope::Exec);
    assert_eq!(
        echo.tool().summarize(&text_input("hi")),
        "mcp__t__echo({\"text\":\"hi\"})"
    );
    server.disconnect().await;
}

#[tokio::test]
async fn untrusted_annotations_leave_every_tool_exec() {
    let duplex = Duplex::new(Fixture::new(standard_tools()));
    let registry = Arc::new(ToolRegistry::new());
    let mut untrusted = config("t", duplex);
    untrusted.trust_annotations = false;
    let server = McpServer::connect(untrusted, Arc::clone(&registry))
        .await
        .unwrap();
    let snapshot = registry.snapshot();
    let echo = snapshot.resolve("mcp__t__echo").unwrap();
    assert_eq!(echo.tool().scope(), ToolScope::Exec);
    server.disconnect().await;
}

#[tokio::test]
async fn calls_map_text_images_and_is_error() {
    let (server, _duplex, registry) = connect(Fixture::new(standard_tools())).await;
    let echoed = call(&registry, "mcp__t__echo", text_input("hello"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("hello"));

    let failed = call(&registry, "mcp__t__fail", JsonObject::new())
        .await
        .unwrap_err();
    assert_eq!(failed.message, "it broke");
    assert_eq!(failed.code.as_deref(), Some(codes::TOOL_ERROR));

    let chart = call(&registry, "mcp__t__image", JsonObject::new())
        .await
        .unwrap();
    assert_eq!(chart.value, json!("chart:\n[image]"));
    assert_eq!(chart.metadata["images"][0]["mime_type"], json!("image/png"));
    server.disconnect().await;
}

#[tokio::test]
async fn a_json_rpc_error_is_a_readable_tool_error() {
    let fixture = Fixture::new(standard_tools());
    // The server lists a tool it cannot run.
    let mut tools = standard_tools();
    tools.push(tool("ghost", "Not implemented."));
    fixture.set_tools(tools);
    let (server, _duplex, registry) = connect(fixture).await;
    let error = call(&registry, "mcp__t__ghost", JsonObject::new())
        .await
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some(codes::PROTOCOL_ERROR));
    assert!(
        error.message.contains("unknown tool ghost"),
        "{}",
        error.message
    );
    server.disconnect().await;
}

async fn relists_on_list_changed(fixture: Fixture) {
    let (server, _duplex, registry) = connect(fixture.clone()).await;
    let before = registry.snapshot();
    let pid_before = Arc::clone(before.resolve("mcp__t__pid").unwrap());

    // echo changes, fail goes, extra arrives; image, pid and slow stay.
    let mut tools = standard_tools();
    tools[0] = tool("echo", "Echo text, louder.");
    tools.remove(1);
    tools.push(tool("extra", "New."));
    fixture.set_tools(tools);

    eventually("the relist", || {
        let registry = Arc::clone(&registry);
        async move { registry.snapshot().resolve("mcp__t__extra").is_some() }
    })
    .await;
    assert_eq!(
        visible(&registry),
        [
            ("mcp__t__echo".to_owned(), "Echo text, louder.".to_owned()),
            ("mcp__t__image".to_owned(), "Return a chart.".to_owned()),
            (
                "mcp__t__pid".to_owned(),
                "Return the server's process id.".to_owned()
            ),
            (
                "mcp__t__slow".to_owned(),
                "Wait until cancelled.".to_owned()
            ),
            ("mcp__t__extra".to_owned(), "New.".to_owned()),
        ]
    );
    // An unchanged tool keeps its registration.
    let after = registry.snapshot();
    assert!(Arc::ptr_eq(
        &pid_before,
        after.resolve("mcp__t__pid").unwrap()
    ));
    server.disconnect().await;
}

#[tokio::test]
async fn list_changed_on_a_subscription_relists() {
    let fixture = Fixture::new(standard_tools());
    relists_on_list_changed(fixture.clone()).await;
    assert_eq!(fixture.subscriptions.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn list_changed_from_a_legacy_server_relists() {
    let fixture = Fixture::new(standard_tools()).legacy();
    relists_on_list_changed(fixture.clone()).await;
    assert_eq!(fixture.subscriptions.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn reconnect_relists_and_calls_fail_readably_meanwhile() {
    let fixture = Fixture::new(standard_tools());
    let duplex = Duplex::new(fixture.clone());
    let registry = Arc::new(ToolRegistry::new());
    let mut patient = config("t", duplex.clone());
    // Enough attempts that the refusal below never exhausts them.
    patient.reconnect.attempts = 1000;
    let server = McpServer::connect(patient, Arc::clone(&registry))
        .await
        .unwrap();
    let mut status = server.watch_status();

    duplex.refuse.store(true, Ordering::SeqCst);
    duplex.kill();
    tokio::time::timeout(
        PATIENCE,
        status.wait_for(|s| *s == ServerStatus::Reconnecting),
    )
    .await
    .unwrap()
    .unwrap();
    let error = call(&registry, "mcp__t__echo", text_input("x"))
        .await
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some(codes::UNAVAILABLE));
    assert!(error.message.contains("reconnecting"), "{}", error.message);

    // Changed while disconnected: no notification can reach the client.
    fixture.set_tools(vec![tool("echo", "Echo text.")]);
    duplex.refuse.store(false, Ordering::SeqCst);
    wait_status(&server, ServerStatus::Connected).await;
    assert!(duplex.connects() >= 2);
    assert_eq!(server.tools(), ["mcp__t__echo"], "relisted after reconnect");
    let echoed = call(&registry, "mcp__t__echo", text_input("back"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("back"));

    // The new connection subscribed again.
    fixture.set_tools(standard_tools());
    eventually("the relist on the new connection", || async {
        server.tools().len() == 5
    })
    .await;
    assert_eq!(fixture.subscriptions.load(Ordering::SeqCst), 2);
    server.disconnect().await;
}

#[tokio::test]
async fn a_lost_connection_during_a_call_reconnects() {
    let fixture = Fixture::new(standard_tools());
    let (server, duplex, registry) = connect(fixture).await;
    let slow = {
        let registry = Arc::clone(&registry);
        tokio::spawn(async move { call(&registry, "mcp__t__slow", JsonObject::new()).await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    duplex.kill();
    let error = tokio::time::timeout(PATIENCE, slow)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some(codes::UNAVAILABLE),
        "{}",
        error.message
    );
    eventually("the reconnect", || async { duplex.connects() >= 2 }).await;
    wait_status(&server, ServerStatus::Connected).await;
    server.disconnect().await;
}

#[tokio::test]
async fn exhausted_reconnects_leave_readable_errors_until_disconnect() {
    let (server, duplex, registry) = connect(Fixture::new(standard_tools())).await;
    duplex.refuse.store(true, Ordering::SeqCst);
    duplex.kill();
    wait_status(&server, ServerStatus::Unavailable).await;
    assert_eq!(duplex.connects(), 1 + 3, "one connect and three attempts");

    let error = call(&registry, "mcp__t__echo", text_input("x"))
        .await
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some(codes::UNAVAILABLE));
    assert!(error.message.contains("unavailable"), "{}", error.message);
    assert_eq!(server.tools().len(), 5, "tools stay registered");

    let mut status = server.watch_status();
    server.disconnect().await;
    assert_eq!(*status.borrow_and_update(), ServerStatus::Closed);
    assert!(visible(&registry).is_empty());
}

#[tokio::test]
async fn cancel_sends_mcp_cancellation() {
    let fixture = Fixture::new(standard_tools());
    let (server, _duplex, registry) = connect(fixture.clone()).await;
    let cancel = CancellationToken::new();
    let call = {
        let registry = Arc::clone(&registry);
        let cancel = cancel.clone();
        tokio::spawn(async move {
            call_with(&registry, "mcp__t__slow", JsonObject::new(), cancel).await
        })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    cancel.cancel();
    let error = tokio::time::timeout(PATIENCE, call)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some(codes::ABORTED));
    tokio::time::timeout(PATIENCE, fixture.cancelled.notified())
        .await
        .expect("the server saw notifications/cancelled");
    assert_eq!(server.status(), ServerStatus::Connected);
    server.disconnect().await;
}

#[tokio::test]
async fn the_call_timeout_cancels_the_request() {
    let fixture = Fixture::new(standard_tools());
    let duplex = Duplex::new(fixture.clone());
    let registry = Arc::new(ToolRegistry::new());
    let mut short = config("t", duplex);
    short.call_timeout = Duration::from_millis(100);
    let server = McpServer::connect(short, Arc::clone(&registry))
        .await
        .unwrap();
    let error = call(&registry, "mcp__t__slow", JsonObject::new())
        .await
        .unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some(codes::TIMEOUT),
        "{}",
        error.message
    );
    tokio::time::timeout(PATIENCE, fixture.cancelled.notified())
        .await
        .expect("the server saw notifications/cancelled");
    server.disconnect().await;
}

#[tokio::test]
async fn a_dropped_call_still_cancels_the_request() {
    let fixture = Fixture::new(standard_tools());
    let (server, _duplex, registry) = connect(fixture.clone()).await;
    // What the scheduler's call timeout does: drop the call's future.
    let slow = call(&registry, "mcp__t__slow", JsonObject::new());
    let dropped = tokio::time::timeout(Duration::from_millis(100), slow).await;
    assert!(dropped.is_err(), "the slow call never answers");
    tokio::time::timeout(PATIENCE, fixture.cancelled.notified())
        .await
        .expect("the server saw notifications/cancelled");
    assert_eq!(server.status(), ServerStatus::Connected);
    server.disconnect().await;
}

#[tokio::test]
async fn the_call_timeout_returns_while_the_server_reads_nothing() {
    let duplex = Duplex::new(Fixture::new(standard_tools()));
    let registry = Arc::new(ToolRegistry::new());
    let mut short = config("t", duplex.clone());
    short.call_timeout = Duration::from_millis(200);
    let server = McpServer::connect(short, Arc::clone(&registry))
        .await
        .unwrap();
    duplex.stall();
    // Larger than the pipe, so this request's write, and every write after
    // it, waits for a reader that never comes.
    let big = text_input(&"x".repeat(256 * 1024));
    let started = std::time::Instant::now();
    let error = tokio::time::timeout(PATIENCE, call(&registry, "mcp__t__echo", big))
        .await
        .expect("the call returned")
        .unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some(codes::TIMEOUT),
        "{}",
        error.message
    );
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "{:?}",
        started.elapsed()
    );
    // A stalled server cannot be closed gracefully; drop it.
    drop(server);
}

#[tokio::test]
async fn dropping_a_server_disposes_its_tools_at_once() {
    let fixture = Fixture::new(standard_tools());
    let registry = Arc::new(ToolRegistry::new());
    let first = McpServer::connect(
        config("t", Duplex::new(fixture.clone())),
        Arc::clone(&registry),
    )
    .await
    .unwrap();
    drop(first);
    assert!(visible(&registry).is_empty(), "still registered");
    // So the same name can connect again straight away.
    let second = McpServer::connect(config("t", Duplex::new(fixture)), Arc::clone(&registry))
        .await
        .unwrap();
    assert!(second.skipped().is_empty(), "{:?}", second.skipped());
    assert_eq!(second.tools().len(), 5);
    second.disconnect().await;
}

#[tokio::test]
async fn disconnect_disposes_only_this_servers_registrations() {
    let registry = Arc::new(ToolRegistry::new());
    let first = McpServer::connect(
        config("a", Duplex::new(Fixture::new(standard_tools()))),
        Arc::clone(&registry),
    )
    .await
    .unwrap();
    let second = McpServer::connect(
        config(
            "b",
            Duplex::new(Fixture::new(vec![tool("echo", "B echo.")])),
        ),
        Arc::clone(&registry),
    )
    .await
    .unwrap();
    assert_eq!(visible(&registry).len(), 6);

    first.disconnect().await;
    assert_eq!(
        visible(&registry),
        [("mcp__b__echo".to_owned(), "B echo.".to_owned())]
    );
    drop(second);
    eventually("the dropped server to close", || {
        let registry = Arc::clone(&registry);
        async move { visible(&registry).is_empty() }
    })
    .await;
}

#[tokio::test]
async fn a_failed_first_connect_is_an_error_and_registers_nothing() {
    let duplex = Duplex::new(Fixture::new(standard_tools()));
    duplex.refuse.store(true, Ordering::SeqCst);
    let registry = Arc::new(ToolRegistry::new());
    let error = McpServer::connect(config("t", duplex.clone()), Arc::clone(&registry))
        .await
        .err()
        .unwrap();
    assert!(
        matches!(error, McpError::Connect { ref server, .. } if server == "t"),
        "{error}"
    );
    assert_eq!(duplex.connects(), 1, "the first connect is not retried");
    assert!(visible(&registry).is_empty());

    let mut bad = config("a__b", duplex);
    bad.trust_annotations = false;
    assert!(matches!(
        McpServer::connect(bad, registry).await,
        Err(McpError::Config(_))
    ));
}
