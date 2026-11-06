//! A connected MCP server: its supervisor task, relist and reconnect.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use linch_core::ToolRegistry;
use rmcp::model::PaginatedRequestParams;
use rmcp::service::{Peer, RoleClient, RunningService, ServiceError};
use thiserror::Error;
use tokio::sync::{Notify, watch};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::catalog::{Catalog, SkippedTool};
use crate::config::{ConfigError, McpServerConfig, ReconnectPolicy};
use crate::connector::{ConnectError, Connector, Handler, connector};
use crate::listener::Listener;
use crate::tool::{Conn, Link, ToolSpec};

/// Most `tools/list` pages read in one listing.
const MAX_LIST_PAGES: usize = 100;

/// Most tools kept from one listing; the rest are left out.
const MAX_TOOLS: usize = 1_000;

/// Where a server's connection stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServerStatus {
    /// Calls go to the server.
    Connected,
    /// The connection was lost; calls fail with a tool error while the
    /// reconnect policy retries.
    Reconnecting,
    /// Every reconnect attempt failed. The tools stay registered and fail
    /// with a tool error until the server is disconnected.
    Unavailable,
    /// Disconnected; every registration is disposed.
    Closed,
}

/// Why [`McpServer::connect`] failed.
#[derive(Debug, Error)]
pub enum McpError {
    /// The configuration is invalid.
    #[error(transparent)]
    Config(#[from] ConfigError),
    /// The first connection attempt failed. Nothing is registered.
    #[error("could not connect to MCP server {server:?}: {source}")]
    Connect {
        /// The server's configured name.
        server: String,
        /// What failed.
        #[source]
        source: Box<ConnectError>,
    },
}

/// One connected MCP server whose tools are in a [`ToolRegistry`].
///
/// A background task keeps the registry in step with the server: on
/// `notifications/tools/list_changed`, and after every reconnect, it lists
/// the tools again and replaces only the ones that changed. Dropping the
/// handle without [`Self::disconnect`] closes the server in the background.
pub struct McpServer {
    link: Arc<Link>,
    catalog: Arc<Mutex<Catalog>>,
    close: CancellationToken,
    task: Option<JoinHandle<()>>,
}

impl McpServer {
    /// Connect to the server, list its tools and register them in `registry`.
    ///
    /// A tool the registry refuses (a name over 64 characters, a name already
    /// taken, an invalid input schema) is left out and reported by
    /// [`Self::skipped`]; it does not fail the connect.
    ///
    /// Errors:
    /// - [`McpError::Config`] when the configuration is invalid;
    /// - [`McpError::Connect`] when the first attempt fails or exceeds the
    ///   reconnect policy's `attempt_timeout`. It is not retried.
    pub async fn connect(
        config: McpServerConfig,
        registry: Arc<ToolRegistry>,
    ) -> Result<Self, McpError> {
        config.validate()?;
        let connector = connector(&config.transport)?;
        let link = Arc::new(Link {
            server: config.name.clone(),
            call_timeout: config.call_timeout,
            conn: watch::Sender::new(None),
            status: watch::Sender::new(ServerStatus::Connected),
        });
        let catalog = Arc::new(Mutex::new(Catalog::default()));
        let supervisor = Supervisor {
            connector,
            registry,
            link: Arc::clone(&link),
            catalog: Arc::clone(&catalog),
            trust_annotations: config.trust_annotations,
            policy: config.reconnect,
        };
        let (live, tools) = supervisor
            .open()
            .await
            .map_err(|source| McpError::Connect {
                server: config.name,
                source: Box::new(source),
            })?;
        supervisor.install(&live, tools);
        let close = CancellationToken::new();
        let task = tokio::spawn(supervisor.run(live, close.clone()));
        Ok(Self {
            link,
            catalog,
            close,
            task: Some(task),
        })
    }

    /// The server's configured name.
    pub fn name(&self) -> &str {
        &self.link.server
    }

    /// Where the connection stands now.
    pub fn status(&self) -> ServerStatus {
        *self.link.status.borrow()
    }

    /// A receiver that sees every status change.
    pub fn watch_status(&self) -> watch::Receiver<ServerStatus> {
        self.link.status.subscribe()
    }

    /// Registry names of the tools this server has registered.
    pub fn tools(&self) -> Vec<String> {
        lock(&self.catalog).names()
    }

    /// Tools from the last listing that the registry refused.
    pub fn skipped(&self) -> Vec<SkippedTool> {
        lock(&self.catalog).skipped()
    }

    /// Close the connection and dispose every registration this server made.
    ///
    /// Waits for the transport to close; a stdio server gets its stdin
    /// closed, then `SIGTERM`, then `SIGKILL`, a few seconds apart.
    pub async fn disconnect(mut self) {
        self.close.cancel();
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for McpServer {
    fn drop(&mut self) {
        self.close.cancel();
    }
}

/// One open connection and what watches it.
struct Live {
    service: RunningService<RoleClient, Handler>,
    lost: CancellationToken,
    list_changed: Arc<Notify>,
    /// Forwards a 2026-07-28 `subscriptions/listen` stream to `list_changed`.
    listener: Option<Listener>,
}

/// The background half of an [`McpServer`].
struct Supervisor {
    connector: Arc<dyn Connector>,
    registry: Arc<ToolRegistry>,
    link: Arc<Link>,
    catalog: Arc<Mutex<Catalog>>,
    trust_annotations: bool,
    policy: ReconnectPolicy,
}

impl Supervisor {
    async fn run(self, mut live: Live, close: CancellationToken) {
        loop {
            self.watch(live, &close).await;
            if close.is_cancelled() {
                break;
            }
            let Some((next, tools)) = self.reconnect(&close).await else {
                break;
            };
            self.install(&next, tools);
            live = next;
        }
        if !close.is_cancelled() {
            self.link.status.send_replace(ServerStatus::Unavailable);
            close.cancelled().await;
        }
        lock(&self.catalog).clear();
        self.link.status.send_replace(ServerStatus::Closed);
    }

    /// Serve one connection until it is lost or closed, then tear it down.
    async fn watch(&self, live: Live, close: &CancellationToken) {
        let Live {
            service,
            lost,
            list_changed,
            listener,
        } = live;
        let peer = service.peer().clone();
        let stop = service.cancellation_token();
        let ended = service.waiting();
        tokio::pin!(ended);
        let ended_by_itself = loop {
            tokio::select! {
                biased;
                () = close.cancelled() => break false,
                () = lost.cancelled() => break false,
                _ = &mut ended => break true,
                () = list_changed.notified() => tokio::select! {
                    biased;
                    () = close.cancelled() => break false,
                    () = lost.cancelled() => break false,
                    _ = &mut ended => break true,
                    listed = tokio::time::timeout(self.policy.attempt_timeout, list_tools(&peer)) => {
                        match listed {
                            Ok(Ok(tools)) => self.sync(tools),
                            Ok(Err(
                                ServiceError::TransportSend(_) | ServiceError::TransportClosed,
                            )) => lost.cancel(),
                            // A failed or timed-out relist keeps the current
                            // tools; the next change or reconnect lists again.
                            _ => {}
                        }
                    }
                },
            }
        };
        // Status first, so a call that finds no connection says why.
        if !close.is_cancelled() {
            self.link.status.send_replace(ServerStatus::Reconnecting);
        }
        self.link.conn.send_replace(None);
        if let Some(listener) = listener {
            listener.stop().await;
        }
        if !ended_by_itself {
            stop.cancel();
            let _ = ended.await;
        }
    }

    async fn reconnect(&self, close: &CancellationToken) -> Option<(Live, Vec<rmcp::model::Tool>)> {
        for attempt in 0..self.policy.attempts {
            tokio::select! {
                () = close.cancelled() => return None,
                () = tokio::time::sleep(self.policy.delay(attempt)) => {}
            }
            tokio::select! {
                () = close.cancelled() => return None,
                opened = self.open() => if let Ok(opened) = opened {
                    return Some(opened);
                },
            }
        }
        None
    }

    /// One attempt: connect, subscribe to tool changes, list the tools.
    async fn open(&self) -> Result<(Live, Vec<rmcp::model::Tool>), ConnectError> {
        let timeout = self.policy.attempt_timeout;
        tokio::time::timeout(timeout, self.open_once())
            .await
            .unwrap_or(Err(ConnectError::Timeout(timeout)))
    }

    async fn open_once(&self) -> Result<(Live, Vec<rmcp::model::Tool>), ConnectError> {
        let list_changed = Arc::new(Notify::new());
        let mut service = self
            .connector
            .connect(Handler::new(Arc::clone(&list_changed)))
            .await?;
        let lost = CancellationToken::new();
        match prepare(service.peer(), &list_changed, &lost).await {
            Ok((listener, tools)) => Ok((
                Live {
                    service,
                    lost,
                    list_changed,
                    listener,
                },
                tools,
            )),
            Err(error) => {
                let _ = service.close().await;
                Err(error)
            }
        }
    }

    /// Route calls to `live`, then register `tools`, so a tool is never
    /// visible before it can be called.
    fn install(&self, live: &Live, tools: Vec<rmcp::model::Tool>) {
        self.link.conn.send_replace(Some(Conn {
            peer: live.service.peer().clone(),
            lost: live.lost.clone(),
        }));
        self.sync(tools);
        self.link.status.send_replace(ServerStatus::Connected);
    }

    fn sync(&self, tools: Vec<rmcp::model::Tool>) {
        let specs = tools
            .iter()
            .map(|tool| ToolSpec::new(&self.link.server, tool, self.trust_annotations))
            .collect();
        lock(&self.catalog).sync(&self.registry, &self.link, specs);
    }
}

/// Subscribe to tool changes when the protocol needs it, then list the
/// tools. Subscribing first means no change is missed in between.
async fn prepare(
    peer: &Peer<RoleClient>,
    list_changed: &Arc<Notify>,
    lost: &CancellationToken,
) -> Result<(Option<Listener>, Vec<rmcp::model::Tool>), ConnectError> {
    let listener = Listener::start(peer, list_changed, lost)
        .await
        .map_err(ConnectError::Listen)?;
    match list_tools(peer).await {
        Ok(tools) => Ok((listener, tools)),
        Err(error) => {
            if let Some(listener) = listener {
                listener.stop().await;
            }
            Err(ConnectError::ListTools(error))
        }
    }
}

/// Follow `tools/list` pages to the end, or until [`MAX_LIST_PAGES`] pages
/// or [`MAX_TOOLS`] tools, so a server cannot keep the client listing or
/// holding tools without end.
async fn list_tools(peer: &Peer<RoleClient>) -> Result<Vec<rmcp::model::Tool>, ServiceError> {
    let mut tools = Vec::new();
    let mut cursor = None;
    for _ in 0..MAX_LIST_PAGES {
        let page = peer
            .list_tools(Some(PaginatedRequestParams::default().with_cursor(cursor)))
            .await?;
        tools.extend(page.tools);
        cursor = page.next_cursor;
        if cursor.is_none() || tools.len() >= MAX_TOOLS {
            break;
        }
    }
    tools.truncate(MAX_TOOLS);
    Ok(tools)
}

fn lock(catalog: &Mutex<Catalog>) -> MutexGuard<'_, Catalog> {
    catalog.lock().unwrap_or_else(PoisonError::into_inner)
}
