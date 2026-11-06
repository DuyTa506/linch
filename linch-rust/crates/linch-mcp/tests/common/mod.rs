//! Shared test helpers: an in-process server over a duplex pipe, and calls.

#![allow(dead_code, unused_imports, reason = "each test binary uses a subset")]

pub mod server;

use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{JsonObject, JsonValue, RunId, SessionId, ToolContext, ToolRegistry, ToolResult};
use linch_mcp::rmcp::ServiceExt;
use linch_mcp::rmcp::service::{RoleClient, RunningService};
use linch_mcp::{
    ConnectError, Connector, Handler, McpServer, McpServerConfig, McpTransport, ReconnectPolicy,
    ServerStatus,
};
use tokio_util::sync::CancellationToken;

pub use server::{Fixture, TestServer, standard_tools, tool};

/// How long a test waits for something asynchronous before failing.
pub const PATIENCE: Duration = Duration::from_secs(10);

/// Dials a fresh in-process [`TestServer`] over a duplex pipe per connect.
pub struct Duplex {
    pub fixture: Fixture,
    pub connects: AtomicUsize,
    /// Whether new connects fail.
    pub refuse: AtomicBool,
    live: Mutex<Option<CancellationToken>>,
}

impl Duplex {
    pub fn new(fixture: Fixture) -> Arc<Self> {
        Arc::new(Self {
            fixture,
            connects: AtomicUsize::new(0),
            refuse: AtomicBool::new(false),
            live: Mutex::new(None),
        })
    }

    /// Cut the live connection, as if the server crashed.
    pub fn kill(&self) {
        let live = self.live.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(stop) = live.as_ref() {
            stop.cancel();
        }
    }

    pub fn connects(&self) -> usize {
        self.connects.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl Connector for Duplex {
    async fn connect(
        &self,
        handler: Handler,
    ) -> Result<RunningService<RoleClient, Handler>, ConnectError> {
        self.connects.fetch_add(1, Ordering::SeqCst);
        if self.refuse.load(Ordering::SeqCst) {
            return Err(ConnectError::Spawn(std::io::Error::other(
                "refused by the test",
            )));
        }
        // client <-> near | relay | far <-> server. Stopping the relay drops
        // both pipes at once, like a crashed server or a dropped network.
        let (client, mut near) = tokio::io::duplex(64 * 1024);
        let (mut far, server) = tokio::io::duplex(64 * 1024);
        let stop = CancellationToken::new();
        *self.live.lock().unwrap_or_else(PoisonError::into_inner) = Some(stop.clone());
        let fixture = self.fixture.clone();
        tokio::spawn(async move {
            if let Ok(service) = TestServer(fixture).serve(server).await {
                let _ = service.waiting().await;
            }
        });
        tokio::spawn(async move {
            tokio::select! {
                _ = tokio::io::copy_bidirectional(&mut near, &mut far) => {}
                () = stop.cancelled() => {}
            }
        });
        handler.serve(client).await
    }
}

/// A config for `connector` with trusted annotations and fast reconnects.
pub fn config(name: &str, connector: Arc<dyn Connector>) -> McpServerConfig {
    let mut config = McpServerConfig::new(name, McpTransport::Custom(connector));
    config.trust_annotations = true;
    config.reconnect = ReconnectPolicy {
        attempts: 3,
        initial_delay: Duration::from_millis(20),
        max_delay: Duration::from_millis(100),
        attempt_timeout: Duration::from_secs(5),
    };
    config
}

/// Connect `fixture` through a fresh [`Duplex`].
pub async fn connect(fixture: Fixture) -> (McpServer, Arc<Duplex>, Arc<ToolRegistry>) {
    let duplex = Duplex::new(fixture);
    let registry = Arc::new(ToolRegistry::new());
    let server = McpServer::connect(config("t", duplex.clone()), Arc::clone(&registry))
        .await
        .expect("connect");
    (server, duplex, registry)
}

pub fn context(cancel: CancellationToken) -> ToolContext {
    ToolContext::new(
        SessionId::from("s"),
        RunId::from("r"),
        "toolu_1",
        None,
        cancel,
        None,
    )
}

pub fn text_input(text: &str) -> JsonObject {
    let mut input = JsonObject::new();
    input.insert("text".into(), JsonValue::from(text));
    input
}

/// Run registry tool `name` with `input`, as the scheduler would.
pub async fn call(registry: &ToolRegistry, name: &str, input: JsonObject) -> ToolResult {
    call_with(registry, name, input, CancellationToken::new()).await
}

pub async fn call_with(
    registry: &ToolRegistry,
    name: &str,
    input: JsonObject,
    cancel: CancellationToken,
) -> ToolResult {
    let snapshot = registry.snapshot();
    let tool = snapshot
        .resolve(name)
        .unwrap_or_else(|| panic!("{name} is not registered"));
    let input = tool.validate_input(input)?;
    tool.tool().execute(input, context(cancel)).await
}

/// Wait until the server reports `status`.
pub async fn wait_status(server: &McpServer, status: ServerStatus) {
    let mut watch = server.watch_status();
    tokio::time::timeout(PATIENCE, watch.wait_for(|now| *now == status))
        .await
        .unwrap_or_else(|_| panic!("status never became {status:?}"))
        .expect("status channel open");
}

/// Poll `check` until it holds.
pub async fn eventually<F, Fut>(what: &str, mut check: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = bool>,
{
    let deadline = tokio::time::Instant::now() + PATIENCE;
    while !check().await {
        assert!(
            tokio::time::Instant::now() < deadline,
            "timed out waiting for {what}"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

/// Registry names and descriptions, in definition order.
pub fn visible(registry: &ToolRegistry) -> Vec<(String, String)> {
    registry
        .snapshot()
        .definitions()
        .into_iter()
        .map(|definition| (definition.name, definition.description))
        .collect()
}
