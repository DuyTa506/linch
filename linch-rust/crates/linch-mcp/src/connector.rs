//! Opening one connection: the stdio and HTTP transports, and the client
//! handler every connection runs.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::process::ExitStatus;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use http::{HeaderName, HeaderValue};
use process_wrap::tokio::{ChildWrapper, CommandWrap, CommandWrapper, ProcessGroup};
use rmcp::model::{ClientCapabilities, ClientConfig, Implementation, ProtocolVersion};
use rmcp::service::{
    ClientInitializeError, NotificationContext, RoleClient, RunningService, ServiceError,
};
use rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig;
use rmcp::transport::{IntoTransport, StreamableHttpClientTransport, TokioChildProcess};
use rmcp::{ClientHandler, ClientLifecycleMode, ClientServiceExt};
use thiserror::Error;
use tokio::sync::Notify;

use crate::config::{ConfigError, McpTransport};

/// Variables a stdio server inherits from this process; the MCP Python SDK
/// uses the same list.
const INHERITED_ENV: [&str; 6] = ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"];

/// Why one connection attempt failed.
#[derive(Debug, Error)]
pub enum ConnectError {
    /// The server process could not be started.
    #[error("could not start the MCP server process: {0}")]
    Spawn(#[source] std::io::Error),
    /// The MCP lifecycle (`server/discover` or `initialize`) failed.
    #[error("MCP handshake failed: {0}")]
    Handshake(#[source] Box<ClientInitializeError>),
    /// The server offers tool list changes but refused the subscription.
    #[error("could not subscribe to MCP tool list changes: {0}")]
    Listen(#[source] ServiceError),
    /// `tools/list` failed.
    #[error("could not list the MCP server's tools: {0}")]
    ListTools(#[source] ServiceError),
    /// The attempt took longer than the policy's `attempt_timeout`.
    #[error("MCP connection attempt timed out after {} s", .0.as_secs_f64())]
    Timeout(Duration),
}

/// The client side of one connection, handed to a [`Connector`].
///
/// It wakes the server's relist loop on `notifications/tools/list_changed`
/// from a server that sends it outside a subscription (protocol versions
/// before 2026-07-28).
#[derive(Clone)]
pub struct Handler {
    list_changed: Arc<Notify>,
}

impl Handler {
    pub(crate) fn new(list_changed: Arc<Notify>) -> Self {
        Self { list_changed }
    }

    /// Run the MCP lifecycle over `transport`.
    ///
    /// Probes with `server/discover` for protocol 2026-07-28 and falls back
    /// to the `initialize` handshake of 2025-11-25 when the server does not
    /// know the method.
    pub async fn serve<T, E, A>(
        self,
        transport: T,
    ) -> Result<RunningService<RoleClient, Handler>, ConnectError>
    where
        T: IntoTransport<RoleClient, E, A>,
        E: std::error::Error + Send + Sync + 'static,
    {
        let lifecycle = ClientLifecycleMode::Auto {
            preferred_versions: vec![ProtocolVersion::V_2026_07_28],
            legacy_version: Some(ProtocolVersion::LATEST_WITH_INITIALIZE),
        };
        self.serve_with_lifecycle(transport, lifecycle)
            .await
            .map_err(|error| ConnectError::Handshake(Box::new(error)))
    }
}

impl ClientHandler for Handler {
    async fn on_tool_list_changed(&self, _context: NotificationContext<RoleClient>) {
        self.list_changed.notify_one();
    }

    fn get_info(&self) -> ClientConfig {
        ClientConfig::new(
            ClientCapabilities::default(),
            Implementation::new("linch", env!("CARGO_PKG_VERSION")),
        )
    }
}

/// Opens a connection to one server. Called once per connect and once per
/// reconnect attempt.
#[async_trait]
pub trait Connector: Send + Sync {
    /// Open a transport and run [`Handler::serve`] over it.
    async fn connect(
        &self,
        handler: Handler,
    ) -> Result<RunningService<RoleClient, Handler>, ConnectError>;
}

/// The connector for `transport`. Fails on an invalid HTTP header.
pub(crate) fn connector(transport: &McpTransport) -> Result<Arc<dyn Connector>, ConfigError> {
    Ok(match transport {
        McpTransport::Stdio { command, args, env } => Arc::new(Stdio {
            command: command.clone(),
            args: args.clone(),
            env: env.clone(),
        }),
        McpTransport::Http { url, headers } => Arc::new(Http {
            url: url.clone(),
            headers: parse_headers(headers)?,
        }),
        McpTransport::Custom(connector) => Arc::clone(connector),
    })
}

struct Stdio {
    command: String,
    args: Vec<String>,
    env: BTreeMap<String, String>,
}

#[async_trait]
impl Connector for Stdio {
    async fn connect(
        &self,
        handler: Handler,
    ) -> Result<RunningService<RoleClient, Handler>, ConnectError> {
        let mut command = tokio::process::Command::new(&self.command);
        command.args(&self.args).env_clear();
        for name in INHERITED_ENV {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        command.envs(&self.env);
        // The server leads its own process group, so what it starts itself
        // (`npx`, `uvx`, `sh -c`) is killed with it.
        let mut command = CommandWrap::from(command);
        command.wrap(ProcessGroup::leader()).wrap(KillGroup);
        let transport = TokioChildProcess::new(command).map_err(ConnectError::Spawn)?;
        handler.serve(transport).await
    }
}

/// Sends `SIGKILL` to a [`ProcessGroup`] child's whole group once the
/// child has exited. rmcp signals the group only when the server outlives
/// its grace period; a server that exits on stdin EOF would otherwise leave
/// its own children running. The group id could in principle be reused in
/// the instant after the server is reaped, as with Python's `killpg`.
#[derive(Debug)]
struct KillGroup;

impl CommandWrapper for KillGroup {
    fn wrap_child(
        &mut self,
        child: Box<dyn ChildWrapper>,
        _: &CommandWrap,
    ) -> std::io::Result<Box<dyn ChildWrapper>> {
        Ok(Box::new(GroupChild(child)))
    }
}

#[derive(Debug)]
struct GroupChild(Box<dyn ChildWrapper>);

impl ChildWrapper for GroupChild {
    fn inner(&self) -> &dyn ChildWrapper {
        self.0.as_ref()
    }

    fn inner_mut(&mut self) -> &mut dyn ChildWrapper {
        self.0.as_mut()
    }

    fn into_inner(self: Box<Self>) -> Box<dyn ChildWrapper> {
        self.0
    }

    fn wait(&mut self) -> Pin<Box<dyn Future<Output = std::io::Result<ExitStatus>> + Send + '_>> {
        Box::pin(async move {
            let status = self.0.wait().await?;
            // The group's `start_kill`; fails harmlessly once it is empty.
            let _ = self.0.start_kill();
            Ok(status)
        })
    }
}

struct Http {
    url: String,
    headers: HashMap<HeaderName, HeaderValue>,
}

#[async_trait]
impl Connector for Http {
    async fn connect(
        &self,
        handler: Handler,
    ) -> Result<RunningService<RoleClient, Handler>, ConnectError> {
        let config = StreamableHttpClientTransportConfig::with_uri(self.url.as_str())
            .custom_headers(self.headers.clone());
        handler
            .serve(StreamableHttpClientTransport::from_config(config))
            .await
    }
}

fn parse_headers(
    headers: &BTreeMap<String, String>,
) -> Result<HashMap<HeaderName, HeaderValue>, ConfigError> {
    headers
        .iter()
        .map(|(name, value)| {
            let invalid = || ConfigError::InvalidHeader { name: name.clone() };
            let parsed_name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| invalid())?;
            let parsed_value = HeaderValue::from_str(value).map_err(|_| invalid())?;
            Ok((parsed_name, parsed_value))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn headers_must_be_valid_http() {
        let mut headers = BTreeMap::new();
        headers.insert("Authorization".to_owned(), "Bearer t".to_owned());
        assert_eq!(parse_headers(&headers).unwrap().len(), 1);

        headers.insert("bad name".to_owned(), "v".to_owned());
        assert_eq!(
            parse_headers(&headers).unwrap_err(),
            ConfigError::InvalidHeader {
                name: "bad name".into()
            }
        );

        let mut bad_value = BTreeMap::new();
        bad_value.insert("X-Key".to_owned(), "line\nbreak".to_owned());
        assert!(parse_headers(&bad_value).is_err());
    }

    #[tokio::test]
    async fn a_missing_program_is_a_spawn_error() {
        let transport = McpTransport::stdio("/nonexistent/linch-mcp-test", Vec::new());
        let connector = connector(&transport).unwrap();
        let error = connector
            .connect(Handler::new(Arc::new(Notify::new())))
            .await
            .err()
            .unwrap();
        assert!(matches!(error, ConnectError::Spawn(_)), "{error}");
    }
}
