//! Bounds on what one MCP server can make the client hold or wait for.

mod common;

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use common::{Duplex, Fixture, PATIENCE, config, connect, eventually, standard_tools, tool};
use linch_core::ToolRegistry;
use linch_mcp::McpServer;

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
