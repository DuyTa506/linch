//! [`Listeners`]: the Unix socket and HTTP listeners, both feeding
//! linch-rpc's connection core, until a stop token fires.

use std::io;
use std::net::SocketAddr;
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use linch::PgPools;
use linch_rpc::{Access, Tokens};
use tokio::net::{TcpListener, UnixListener};
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::ServerConfig;
use crate::http;

/// The bound listeners of a [`ServerConfig`].
#[derive(Debug)]
pub struct Listeners {
    socket: Option<(UnixListener, PathBuf)>,
    http: Option<TcpListener>,
    tokens: Arc<Tokens>,
}

impl Listeners {
    /// Bind every listener `config` names, checking each connection
    /// against `tokens`.
    ///
    /// A socket file left by a server that is no longer running is
    /// replaced; a path in use by a running server, or that is not a
    /// socket, is an error.
    ///
    /// Args:
    ///     config: Names the socket path and the HTTP address.
    ///     tokens: The bearer tokens, from [`ServerConfig::tokens`].
    ///
    /// Raises:
    ///     io::Error: If a listener cannot be bound.
    pub async fn bind(config: &ServerConfig, tokens: Tokens) -> io::Result<Self> {
        let socket = match &config.socket {
            Some(path) => Some((bind_socket(path)?, path.clone())),
            None => None,
        };
        let http = match config.http {
            Some(address) => Some(TcpListener::bind(address).await?),
            None => None,
        };
        Ok(Self {
            socket,
            http,
            tokens: Arc::new(tokens),
        })
    }

    /// The socket path, when the config names one.
    pub fn socket_path(&self) -> Option<&Path> {
        self.socket.as_ref().map(|(_, path)| path.as_path())
    }

    /// The bound HTTP address (with the real port when the config asked
    /// for port 0).
    pub fn http_addr(&self) -> Option<SocketAddr> {
        self.http
            .as_ref()
            .and_then(|listener| listener.local_addr().ok())
    }

    /// Serve every connection until `stop` fires, then stop accepting,
    /// end every connection as at the end of its input (runs dropped,
    /// calls in flight answered, MCP servers closed), wait for them, and
    /// remove the socket file. Agents of every connection on one Postgres
    /// DSN share one pool. A failed accept is reported on stderr and
    /// retried, and a connection's I/O error ends only that connection.
    pub async fn serve(self, stop: CancellationToken) {
        let connections = TaskTracker::new();
        let pools = Arc::new(PgPools::new());
        let socket = async {
            if let Some((listener, _)) = &self.socket {
                accept(listener, &self.tokens, &pools, &connections, &stop).await;
            }
        };
        let web = async {
            if let Some(listener) = self.http {
                let shared = http::Shared {
                    tokens: Arc::clone(&self.tokens),
                    pools: Arc::clone(&pools),
                    connections: connections.clone(),
                };
                http::serve(listener, shared, stop.clone()).await;
            }
        };
        tokio::join!(socket, web);
        connections.close();
        connections.wait().await;
        if let Some((_, path)) = &self.socket {
            let _ = std::fs::remove_file(path);
        }
    }
}

/// Bind `path`, replacing a socket file no server answers on.
fn bind_socket(path: &Path) -> io::Result<UnixListener> {
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_socket() => {
            if std::os::unix::net::UnixStream::connect(path).is_ok() {
                let message = format!("{} is in use by a running server", path.display());
                return Err(io::Error::new(io::ErrorKind::AddrInUse, message));
            }
            std::fs::remove_file(path)?;
        }
        Ok(_) => {
            let message = format!("{} exists and is not a socket", path.display());
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, message));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    UnixListener::bind(path)
}

/// Accept socket connections until `stop`; each must log in first.
async fn accept(
    listener: &UnixListener,
    tokens: &Arc<Tokens>,
    pools: &Arc<PgPools>,
    connections: &TaskTracker,
    stop: &CancellationToken,
) {
    loop {
        let accepted = tokio::select! {
            biased;
            () = stop.cancelled() => return,
            accepted = listener.accept() => accepted,
        };
        let stream = match accepted {
            Ok((stream, _)) => stream,
            Err(error) => {
                // Out of descriptors, say: wait a moment rather than spin.
                eprintln!("linch-server: socket accept failed: {error}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let (reader, writer) = stream.into_split();
        let access = Access::Login(Arc::clone(tokens));
        let pools = Arc::clone(pools);
        let stop = stop.clone();
        connections.spawn(async move {
            // A connection's I/O error ends only that connection.
            let _ = linch_rpc::serve_lines(reader, writer, access, pools, stop).await;
        });
    }
}
