//! Round trips over streamable HTTP to a server on 127.0.0.1.

mod common;

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::Request;
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use common::{
    Fixture, PATIENCE, TestServer, call, call_with, eventually, standard_tools, text_input, tool,
};
use linch_core::{JsonObject, ToolRegistry};
use linch_mcp::rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use linch_mcp::rmcp::transport::streamable_http_server::{
    StreamableHttpServerConfig, StreamableHttpService,
};
use linch_mcp::{McpServer, McpServerConfig, McpTransport, codes};
use serde_json::json;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

/// The fixture served at `/mcp` on a free port.
struct Http {
    url: String,
    stop: CancellationToken,
    task: JoinHandle<std::io::Result<()>>,
}

impl Http {
    async fn start(fixture: &Fixture, router: impl FnOnce(axum::Router) -> axum::Router) -> Self {
        let stop = CancellationToken::new();
        let service: StreamableHttpService<TestServer, LocalSessionManager> =
            StreamableHttpService::new(
                {
                    let fixture = fixture.clone();
                    move || Ok(TestServer(fixture.clone()))
                },
                Default::default(),
                StreamableHttpServerConfig::default().with_cancellation_token(stop.child_token()),
            );
        let router = router(axum::Router::new().nest_service("/mcp", service));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn({
            let stop = stop.clone();
            async move {
                axum::serve(listener, router)
                    .with_graceful_shutdown(stop.cancelled_owned())
                    .await
            }
        });
        Self {
            url: format!("http://{address}/mcp"),
            stop,
            task,
        }
    }

    async fn stop(self) {
        self.stop.cancel();
        tokio::time::timeout(PATIENCE, self.task)
            .await
            .expect("the HTTP server shut down")
            .unwrap()
            .unwrap();
    }
}

#[tokio::test]
async fn http_round_trip_list_changed_and_cancel() {
    let fixture = Fixture::new(standard_tools());
    let http = Http::start(&fixture, |router| router).await;
    let registry = Arc::new(ToolRegistry::new());
    let mut headers = BTreeMap::new();
    headers.insert("X-Linch-Test".to_owned(), "1".to_owned());
    let transport = McpTransport::Http {
        url: http.url.clone(),
        headers,
    };
    let server = McpServer::connect(
        McpServerConfig::new("web", transport),
        Arc::clone(&registry),
    )
    .await
    .expect("connect over HTTP");

    let echoed = call(&registry, "mcp__web__echo", text_input("over http"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("over http"));
    let failed = call(&registry, "mcp__web__fail", JsonObject::new())
        .await
        .unwrap_err();
    assert_eq!(failed.code.as_deref(), Some(codes::TOOL_ERROR));

    // list_changed arrives on the subscription's HTTP stream.
    let mut tools = standard_tools();
    tools.push(tool("extra", "New."));
    fixture.set_tools(tools);
    eventually("the relist over HTTP", || async {
        server.tools().contains(&"mcp__web__extra".to_owned())
    })
    .await;

    // Cancelling closes the request's stream, which the server treats as
    // cancellation.
    let cancel = CancellationToken::new();
    let slow = {
        let registry = Arc::clone(&registry);
        let cancel = cancel.clone();
        tokio::spawn(async move {
            call_with(&registry, "mcp__web__slow", JsonObject::new(), cancel).await
        })
    };
    tokio::time::sleep(Duration::from_millis(100)).await;
    cancel.cancel();
    let aborted = tokio::time::timeout(PATIENCE, slow)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(aborted.code.as_deref(), Some(codes::ABORTED));
    tokio::time::timeout(PATIENCE, fixture.cancelled.notified())
        .await
        .expect("the server saw the cancellation");

    server.disconnect().await;
    assert!(registry.snapshot().definitions().is_empty());
    http.stop().await;
}

/// Answer 500 to a POST that mentions "boom", as a gateway or a crashed
/// handler would; pass everything else on.
async fn fail_boom(request: Request, next: Next) -> Response {
    let (parts, body) = request.into_parts();
    let bytes = axum::body::to_bytes(body, usize::MAX).await.unwrap();
    if bytes.windows(4).any(|window| window == b"boom") {
        return (StatusCode::INTERNAL_SERVER_ERROR, "boom").into_response();
    }
    next.run(Request::from_parts(parts, Body::from(bytes)))
        .await
}

#[tokio::test]
async fn an_http_error_on_one_post_fails_only_that_call() {
    let fixture = Fixture::new(standard_tools());
    let http = Http::start(&fixture, |router| {
        router.layer(middleware::from_fn(fail_boom))
    })
    .await;
    let registry = Arc::new(ToolRegistry::new());
    let transport = McpTransport::Http {
        url: http.url.clone(),
        headers: BTreeMap::new(),
    };
    let server = McpServer::connect(
        McpServerConfig::new("web", transport),
        Arc::clone(&registry),
    )
    .await
    .expect("connect over HTTP");
    let mut status = server.watch_status();
    status.borrow_and_update();

    let cancel = CancellationToken::new();
    let slow = {
        let registry = Arc::clone(&registry);
        let cancel = cancel.clone();
        tokio::spawn(async move {
            call_with(&registry, "mcp__web__slow", JsonObject::new(), cancel).await
        })
    };
    tokio::time::sleep(Duration::from_millis(100)).await;
    let failed = call(&registry, "mcp__web__echo", text_input("boom"))
        .await
        .unwrap_err();
    assert_eq!(failed.code.as_deref(), Some(codes::UNAVAILABLE));
    assert!(failed.message.contains("500"), "{}", failed.message);

    // The connection stays up: the other call runs on and new calls work.
    let echoed = call(&registry, "mcp__web__echo", text_input("fine"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("fine"));
    assert!(!slow.is_finished(), "the in-flight call was failed too");
    assert!(!status.has_changed().unwrap(), "the status changed");
    cancel.cancel();
    let aborted = tokio::time::timeout(PATIENCE, slow)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(aborted.code.as_deref(), Some(codes::ABORTED));

    server.disconnect().await;
    http.stop().await;
}
