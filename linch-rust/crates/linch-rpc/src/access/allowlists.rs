//! [`Allowlists`]: what a tenant's `agent.open` may ask for.

use std::path::PathBuf;
use std::str::FromStr;

use linch::PermissionMode;
use sqlx::postgres::PgConnectOptions;
use url::{Origin, Url};

use super::AccessError;

/// What a tenant's `agent.open` may ask for, built with the methods
/// below. A new value allows the memory stores, the permission modes
/// `default` and `accept_edits`, the live providers at their public
/// endpoints, and nothing else.
#[derive(Debug)]
pub struct Allowlists {
    pub(super) scripted_provider: bool,
    pub(super) provider_origins: Vec<Origin>,
    pub(super) permission_modes: Vec<PermissionMode>,
    pub(super) workspace_roots: Vec<PathBuf>,
    pub(super) postgres: Vec<PgTarget>,
    pub(super) mcp_commands: Vec<String>,
    pub(super) mcp_origins: Vec<Origin>,
}

/// Where a Postgres DSN connects: what an allowed DSN must match.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct PgTarget {
    host: String,
    port: u16,
    socket: Option<PathBuf>,
    database: Option<String>,
}

impl PgTarget {
    /// Read `dsn` the way the store connects with it, so a `?host=`
    /// override counts.
    pub(super) fn parse(dsn: &str) -> Option<Self> {
        let options = PgConnectOptions::from_str(dsn).ok()?;
        Some(Self {
            host: options.get_host().to_owned(),
            port: options.get_port(),
            socket: options.get_socket().cloned(),
            database: options.get_database().map(str::to_owned),
        })
    }
}

impl Default for Allowlists {
    fn default() -> Self {
        Self::new()
    }
}

impl Allowlists {
    /// The defaults described on [`Allowlists`].
    pub fn new() -> Self {
        Self {
            scripted_provider: false,
            provider_origins: Vec::new(),
            permission_modes: vec![PermissionMode::Default, PermissionMode::AcceptEdits],
            workspace_roots: Vec::new(),
            postgres: Vec::new(),
            mcp_commands: Vec::new(),
            mcp_origins: Vec::new(),
        }
    }

    /// Whether the `scripted` provider is allowed (for tests and evals).
    pub fn scripted_provider(mut self, allowed: bool) -> Self {
        self.scripted_provider = allowed;
        self
    }

    /// Endpoints a provider `base_url` may name, so they receive the
    /// server's key: `http` or `https` URLs, matched on scheme, host and
    /// port; the path does not matter.
    ///
    /// Raises:
    ///     AccessError: If an entry is not an `http` or `https` URL.
    pub fn provider_base_urls(mut self, urls: &[String]) -> Result<Self, AccessError> {
        self.provider_origins = origins("provider_base_urls", urls)?;
        Ok(self)
    }

    /// The permission modes a config may choose, replacing the default
    /// `default` and `accept_edits`.
    pub fn permission_modes(mut self, modes: &[PermissionMode]) -> Self {
        self.permission_modes = modes.to_vec();
        self
    }

    /// Directories a `workspace` may be, or lie under, compared on the
    /// canonical path. Blocks on the file system, so call it at startup.
    ///
    /// Raises:
    ///     AccessError: If a root does not exist or cannot be resolved.
    pub fn workspace_roots(mut self, roots: &[PathBuf]) -> Result<Self, AccessError> {
        self.workspace_roots = (roots.iter())
            .map(|root| {
                std::fs::canonicalize(root).map_err(|source| AccessError::WorkspaceRoot {
                    root: root.clone(),
                    source,
                })
            })
            .collect::<Result<_, _>>()?;
        Ok(self)
    }

    /// Postgres servers a store DSN may connect to: a DSN is allowed when
    /// its host, port (or socket) and database equal an entry's.
    /// Credentials and options are the client's own.
    ///
    /// Raises:
    ///     AccessError: If an entry is not a valid DSN.
    pub fn postgres_dsns(mut self, dsns: &[String]) -> Result<Self, AccessError> {
        self.postgres = (dsns.iter().enumerate())
            .map(|(index, dsn)| {
                PgTarget::parse(dsn).ok_or(AccessError::Url {
                    list: "postgres_dsns",
                    index,
                })
            })
            .collect::<Result<_, _>>()?;
        Ok(self)
    }

    /// Programs an MCP `stdio` transport may run, each compared with
    /// `command` exactly. Its `args` and `env` are the client's.
    pub fn mcp_commands(mut self, commands: &[String]) -> Self {
        self.mcp_commands = commands.to_vec();
        self
    }

    /// Endpoints an MCP `http` transport may name, matched like
    /// [`Self::provider_base_urls`].
    ///
    /// Raises:
    ///     AccessError: If an entry is not an `http` or `https` URL.
    pub fn mcp_urls(mut self, urls: &[String]) -> Result<Self, AccessError> {
        self.mcp_origins = origins("mcp_urls", urls)?;
        Ok(self)
    }
}

fn origins(list: &'static str, urls: &[String]) -> Result<Vec<Origin>, AccessError> {
    (urls.iter().enumerate())
        .map(|(index, url)| match Url::parse(url) {
            Ok(url) if matches!(url.scheme(), "http" | "https") => Ok(url.origin()),
            _ => Err(AccessError::Url { list, index }),
        })
        .collect()
}

/// Whether `url` shares an origin with one of `allowed`.
pub(super) fn allows(allowed: &[Origin], url: &str) -> bool {
    Url::parse(url).is_ok_and(|url| allowed.contains(&url.origin()))
}
