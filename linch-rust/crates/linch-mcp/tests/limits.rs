//! Bounds on what one MCP server can make the client hold or wait for.

mod common;

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use common::{
    Duplex, Fixture, PATIENCE, call, config, connect, eventually, standard_tools, text_input, tool,
    tool_described,
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

/// A JSON-RPC error's message reaches the model and the session log like a
/// result's text, so it is cut like one.
#[tokio::test]
async fn a_json_rpc_error_message_is_capped_like_result_text() {
    let (server, _duplex, registry) =
        connect(Fixture::new(vec![tool("rpc_error", "Fails.")])).await;
    let error = call(&registry, "mcp__t__rpc_error", text_input("x"))
        .await
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some(codes::PROTOCOL_ERROR));
    assert!(
        error.message.len() <= 31_000,
        "the tool error text is {} bytes",
        error.message.len()
    );
    server.disconnect().await;
}

/// A listing past the byte bound is cut like one past the tool count: the
/// tools that fit, in order, are kept and the rest are left out.
#[tokio::test]
async fn a_tool_list_over_the_byte_bound_is_cut_in_order() {
    let huge = (0..8)
        .map(|i| tool_described(format!("t{i}"), "d".repeat(1024 * 1024)))
        .collect();
    let (server, _duplex, _registry) = connect(Fixture::new(huge)).await;
    assert_eq!(server.tools(), ["mcp__t__t0", "mcp__t__t1", "mcp__t__t2"]);
    server.disconnect().await;
}

/// The bound counts the whole tool as received, not only the fields the
/// model sees: a megabyte of `title`, `_meta` or `outputSchema` is held in
/// the listing all the same.
#[tokio::test]
async fn megabytes_in_the_other_fields_of_a_tool_count_toward_the_byte_bound() {
    let titled = (0..8)
        .map(|i| {
            let mut tool = tool(format!("t{i}"), "Small.");
            tool.title = Some("t".repeat(1024 * 1024));
            tool
        })
        .collect();
    let (server, _duplex, _registry) = connect(Fixture::new(titled)).await;
    assert_eq!(server.tools(), ["mcp__t__t0", "mcp__t__t1", "mcp__t__t2"]);
    server.disconnect().await;
}

/// A server whose first tool alone is over the bound cannot be used.
#[tokio::test]
async fn a_first_tool_over_the_byte_bound_refuses_the_server() {
    let mut huge = tool("t0", "Small.");
    huge.title = Some("t".repeat(5 * 1024 * 1024));
    let registry = Arc::new(ToolRegistry::new());
    let duplex = Duplex::new(Fixture::new(vec![huge, tool("t1", "Small.")]));
    let connected = McpServer::connect(config("t", duplex), Arc::clone(&registry)).await;
    let error = connected.err().expect("the listing is refused");
    assert!(error.to_string().contains("bytes"), "{error}");
    assert!(registry.snapshot().definitions().is_empty());
}

/// A relist over the byte bound is cut like the first listing.
#[tokio::test]
async fn a_relist_over_the_byte_bound_is_cut_in_order() {
    let fixture = Fixture::new(standard_tools());
    let (server, _duplex, _registry) = connect(fixture.clone()).await;
    fixture.set_tools(
        (0..8)
            .map(|i| tool_described(format!("t{i}"), "d".repeat(1024 * 1024)))
            .collect(),
    );
    eventually("the cut listing", || async {
        server.tools() == ["mcp__t__t0", "mcp__t__t1", "mcp__t__t2"]
    })
    .await;
    server.disconnect().await;
}

/// A relist whose first tool alone is over the bound keeps the tools the
/// server has, like a relist that failed.
#[tokio::test]
async fn a_relist_with_a_first_tool_over_the_byte_bound_keeps_the_current_tools() {
    let fixture = Fixture::new(standard_tools());
    let (server, _duplex, _registry) = connect(fixture.clone()).await;
    let before = fixture.lists.load(Ordering::SeqCst);
    let mut huge = tool("t0", "Small.");
    huge.title = Some("t".repeat(5 * 1024 * 1024));
    fixture.set_tools(vec![huge]);
    eventually("the relist to reach the server", || async {
        fixture.lists.load(Ordering::SeqCst) > before
    })
    .await;
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        server.tools().contains(&"mcp__t__echo".to_owned()),
        "{:?}",
        server.tools()
    );
    server.disconnect().await;
}

/// A server that sends a burst of `list_changed` costs a few listings, not
/// one per notification, and the last change is still applied.
#[tokio::test]
async fn a_burst_of_list_changes_is_relisted_a_few_times() {
    let fixture = Fixture::new(standard_tools());
    let (server, _duplex, _registry) = connect(fixture.clone()).await;
    let before = fixture.lists.load(Ordering::SeqCst);
    let began = Instant::now();
    for i in 0..20 {
        fixture.set_tools(vec![tool(format!("n{i}"), "Changing.")]);
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    eventually("the last change to be listed", || async {
        server.tools() == ["mcp__t__n19"]
    })
    .await;
    let lists = fixture.lists.load(Ordering::SeqCst) - before;
    // One at once, then at most one a second while the burst lasts.
    let allowed = 3 + began.elapsed().as_secs() as usize;
    assert!(
        lists <= allowed,
        "{lists} listings for 20 notifications, {allowed} allowed"
    );
    server.disconnect().await;
}
