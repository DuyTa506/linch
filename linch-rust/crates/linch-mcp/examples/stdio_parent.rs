//! A process that holds one stdio MCP connection open until it is killed,
//! for the test that kills it with `SIGKILL`.
//!
//! Arguments: a file to write the server's pid to, and the path of the
//! `stdio_server` example. The "server" is a shell that ignores `SIGTERM`
//! and, once the example server exits at the end of its input, keeps
//! running, so only a kill ends it. The line `ready` on stdout says the
//! connection is up.

use std::sync::Arc;

use linch_core::ToolRegistry;
use linch_mcp::{McpServer, McpServerConfig, McpTransport};

#[tokio::main]
async fn main() {
    let mut args = std::env::args().skip(1);
    let pid_file = args.next().expect("pid file argument");
    let server = args.next().expect("server path argument");
    let script = r#"echo $$ >"$0"; trap '' TERM; "$1"; exec sleep 3600"#;
    let config = McpServerConfig::new(
        "stubborn",
        McpTransport::stdio(
            "/bin/sh",
            vec!["-c".into(), script.into(), pid_file, server],
        ),
    );
    let _server = McpServer::connect(config, Arc::new(ToolRegistry::new()))
        .await
        .expect("connect over stdio");
    println!("ready");
    std::future::pending::<()>().await;
}
