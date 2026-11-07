//! Bounds on what one MCP server can make the client hold or wait for.

mod common;

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use common::{
    Duplex, Fixture, PATIENCE, call, config, connect, eventually, standard_tools, text_input, tool,
};
use linch_core::ToolRegistry;
use linch_mcp::{McpServer, codes};

#[tokio::test]
async fn an_endless_tool_list_is_cut_off_at_one_hundred_pages() {
    let (server, _duplex, _registry) = connect(Fixture::new(Vec::new()).endless()).await;
    let tools = server.tools();
    assert_eq!(tools.len(), 100);
    assert_eq!(tools.last().map(String::as_str), Some("mcp__t__p99"));
    server.disconnect().await;
}

#[tokio::test]
async fn one_listing_keeps_at_most_one_thousand_tools() {
    let many = (0..1_500).map(|i| tool(format!("t{i}"), "Many.")).collect();
    let (server, _duplex, _registry) = connect(Fixture::new(many)).await;
    let tools = server.tools();
    assert_eq!(tools.len(), 1_000);
    assert_eq!(tools.last().map(String::as_str), Some("mcp__t__t999"));
    server.disconnect().await;
}

#[tokio::test]
async fn a_relist_that_never_answers_times_out() {
    let fixture = Fixture::new(standard_tools());
    let registry = Arc::new(ToolRegistry::new());
    let mut quick = config("t", Duplex::new(fixture.clone()));
    // Long enough for the first connect on a busy machine, short enough
    // that the hung relist below times out quickly.
    quick.reconnect.attempt_timeout = Duration::from_secs(2);
    let server = McpServer::connect(quick, Arc::clone(&registry))
        .await
        .unwrap();

    fixture.hang_lists.store(true, Ordering::SeqCst);
    fixture.set_tools(vec![tool("first", "Never listed.")]);
    tokio::time::timeout(PATIENCE, fixture.hung.notified())
        .await
        .expect("the relist reached the server");
    fixture.hang_lists.store(false, Ordering::SeqCst);
    fixture.set_tools(vec![tool("second", "Listed.")]);
    eventually("the relist after the hung one", || async {
        server.tools() == ["mcp__t__second"]
    })
    .await;
    server.disconnect().await;
}

#[tokio::test]
async fn cancels_waiting_on_a_stalled_server_are_capped() {
    let duplex = Duplex::new(Fixture::new(standard_tools()));
    let registry = Arc::new(ToolRegistry::new());
    let mut short = config("t", duplex.clone());
    short.call_timeout = Duration::from_millis(200);
    let server = McpServer::connect(short, Arc::clone(&registry))
        .await
        .unwrap();
    duplex.stall();
    // Larger than the pipe, so every write after this one waits.
    let big = text_input(&"x".repeat(256 * 1024));
    call(&registry, "mcp__t__echo", big).await.unwrap_err();

    let metrics = tokio::runtime::Handle::current().metrics();
    let before = metrics.num_alive_tasks();
    const CALLS: usize = 400;
    let mut calls = tokio::task::JoinSet::new();
    for _ in 0..CALLS {
        let registry = Arc::clone(&registry);
        calls.spawn(async move { call(&registry, "mcp__t__echo", text_input("x")).await });
    }
    while let Some(result) = calls.join_next().await {
        let error = result.unwrap().unwrap_err();
        assert_eq!(error.code.as_deref(), Some(codes::TIMEOUT));
    }
    // Each call leaves rmcp a task that waits to send its request. A cancel
    // task per call on top of that would be two or three tasks per call.
    let grown = metrics.num_alive_tasks().saturating_sub(before);
    assert!(
        grown < 2 * CALLS,
        "{grown} tasks for {CALLS} abandoned calls"
    );

    drop(server);
    duplex.kill();
}
