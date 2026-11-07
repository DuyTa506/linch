//! The test MCP server on stdin and stdout, for the stdio round-trip test.
//! It exits when stdin closes.

#[allow(dead_code, reason = "the example uses a subset of the test server")]
#[path = "../tests/common/server.rs"]
mod server;

use rmcp::ServiceExt;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let fixture = server::Fixture::new(server::standard_tools());
    let service = server::TestServer(fixture)
        .serve(rmcp::transport::stdio())
        .await
        .expect("serve on stdio");
    let _ = service.waiting().await;
}
