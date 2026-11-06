//! [`ServerConfig`]: the listeners, the tenants with their bearer tokens,
//! and each tenant's allowlists, as one strict JSON value.

use std::collections::BTreeMap;
use std::fmt;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use linch::PermissionMode;
use linch_rpc::{AccessError, Allowlists, McpCommand, Tenant, Tokens};
use serde::Deserialize;
use thiserror::Error;

/// What `linch-server --config FILE` serves.
///
/// Decoding rejects unknown fields at every level. `Debug` never shows a
/// token.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    /// Path of a Unix socket to listen on.
    #[serde(default)]
    pub socket: Option<PathBuf>,
    /// Address for HTTP with WebSocket, for example `127.0.0.1:8731`.
    #[serde(default)]
    pub http: Option<SocketAddr>,
    /// Every tenant by name.
    pub tenants: BTreeMap<String, TenantConfig>,
}

/// One tenant: the tokens that name it and what its agents may use.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TenantConfig {
    /// Bearer tokens, each given inline or by environment variable.
    pub tokens: Vec<TokenSource>,
    /// The tenant's allowlists; every list empty when absent.
    #[serde(default)]
    pub allow: AllowConfig,
}

/// Where a bearer token comes from. `Debug` hides an inline value.
#[derive(Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum TokenSource {
    /// `{"env": "NAME"}`: the value of an environment variable of the
    /// server, read at startup.
    Env(String),
    /// `{"value": "..."}`: the token itself.
    Value(String),
}

impl fmt::Debug for TokenSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Env(name) => f.debug_tuple("Env").field(name).finish(),
            Self::Value(_) => f.write_str("Value([redacted])"),
        }
    }
}

/// The serde form of [`Allowlists`]; see its builder methods for how each
/// list matches.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AllowConfig {
    /// Whether the `scripted` provider is allowed (for tests and evals).
    pub scripted_provider: bool,
    /// Whether a workspace agent gets `Bash`, which runs commands
    /// unconfined, as the server's user; off when absent.
    pub exec_tools: bool,
    /// Runs the tenant's connections may drive at once, together, at
    /// least 1; [`linch_rpc::DEFAULT_MAX_RUNS`] when absent.
    pub max_runs: Option<usize>,
    /// Connections that may act for the tenant at once, at least 1;
    /// [`linch_rpc::DEFAULT_MAX_CONNECTIONS`] when absent.
    pub max_connections: Option<usize>,
    /// Agents the tenant's connections may keep open at once, together,
    /// at least 1; [`linch_rpc::DEFAULT_MAX_AGENTS`] when absent.
    pub max_agents: Option<usize>,
    /// Endpoints a provider `base_url` may name.
    pub provider_base_urls: Vec<String>,
    /// Permission modes a config may choose; `default` and `accept_edits`
    /// when absent. `auto_allow` and rules that allow need
    /// `skip_dangerous` here.
    pub permission_modes: Option<Vec<PermissionMode>>,
    /// Directories a workspace and a project root may be or lie under.
    pub workspace_roots: Vec<PathBuf>,
    /// Postgres servers, databases, users, options and TLS files a store
    /// DSN may use, and the weakest `sslmode` it may ask for.
    pub postgres_dsns: Vec<String>,
    /// MCP `stdio` servers a transport may start, each
    /// `{"command", "args"?, "env"?}`: the program and its arguments, and
    /// the names of the variables a transport may set.
    pub mcp_commands: Vec<McpCommand>,
    /// Endpoints an MCP `http` transport may name.
    pub mcp_urls: Vec<String>,
}

/// The server config could not be read or set up. No message holds a
/// token.
#[derive(Debug, Error)]
pub enum ConfigError {
    /// The file could not be read.
    #[error("cannot read the config: {0}")]
    Read(#[source] std::io::Error),
    /// The JSON is malformed or has an unknown or mistyped field.
    #[error("invalid config: {0}")]
    Json(#[source] serde_json::Error),
    /// Neither `socket` nor `http` is set.
    #[error("the config sets neither socket nor http")]
    NoListener,
    /// No tenant has a token, so every connection would be refused.
    #[error("the config gives no tenant a token")]
    NoToken,
    /// A tenant's `max_runs`, `max_connections` or `max_agents` is 0.
    #[error(
        "tenant {tenant:?}: {field} is 0, which lets it do nothing; \
         set at least 1, or leave it out for the default"
    )]
    ZeroCap {
        /// The tenant.
        tenant: String,
        /// `max_runs`, `max_connections` or `max_agents`.
        field: &'static str,
    },
    /// A token's environment variable is unset or not UTF-8.
    #[error("tenant {tenant:?}: environment variable {var} is unset")]
    TokenEnv {
        /// The tenant the token is for.
        tenant: String,
        /// The variable.
        var: String,
    },
    /// A tenant name, token or allowlist entry is invalid.
    #[error("tenant {tenant:?}: {source}")]
    Access {
        /// The tenant whose entry is invalid.
        tenant: String,
        /// What is wrong with it.
        #[source]
        source: AccessError,
    },
}

impl ServerConfig {
    /// Read a config from its JSON text.
    ///
    /// Raises:
    ///     ConfigError: If the JSON is invalid, sets no listener, or gives
    ///         a tenant a cap of 0.
    pub fn from_json(text: &str) -> Result<Self, ConfigError> {
        let config: Self = serde_json::from_str(text).map_err(ConfigError::Json)?;
        if config.socket.is_none() && config.http.is_none() {
            return Err(ConfigError::NoListener);
        }
        for (name, tenant) in &config.tenants {
            let allow = &tenant.allow;
            let caps = [
                ("max_runs", allow.max_runs),
                ("max_connections", allow.max_connections),
                ("max_agents", allow.max_agents),
            ];
            if let Some((field, _)) = caps.into_iter().find(|(_, cap)| *cap == Some(0)) {
                let tenant = name.clone();
                return Err(ConfigError::ZeroCap { tenant, field });
            }
        }
        Ok(config)
    }

    /// Build every tenant and its tokens, reading `{"env": ...}` tokens
    /// with `env`. Blocks on the file system (workspace roots are
    /// canonicalized), so call it at startup.
    ///
    /// Args:
    ///     env: Looks up one environment variable, for example
    ///         `|name| std::env::var(name).ok()`.
    ///
    /// Returns:
    ///     The token table the listeners check every connection against.
    ///
    /// Raises:
    ///     ConfigError: If a variable is unset, a tenant, token or
    ///         allowlist entry is invalid, or no tenant has a token.
    pub fn tokens(&self, env: impl Fn(&str) -> Option<String>) -> Result<Tokens, ConfigError> {
        let mut tokens = Tokens::new();
        for (name, tenant) in &self.tenants {
            let access = |source| ConfigError::Access {
                tenant: name.clone(),
                source,
            };
            let built =
                Arc::new(Tenant::new(name, tenant.allow.build().map_err(access)?).map_err(access)?);
            for source in &tenant.tokens {
                let token = match source {
                    TokenSource::Value(token) => token.clone(),
                    TokenSource::Env(var) => env(var).ok_or_else(|| ConfigError::TokenEnv {
                        tenant: name.clone(),
                        var: var.clone(),
                    })?,
                };
                tokens.insert(&token, Arc::clone(&built)).map_err(access)?;
            }
        }
        if tokens.is_empty() {
            return Err(ConfigError::NoToken);
        }
        Ok(tokens)
    }
}

impl AllowConfig {
    /// The [`Allowlists`] this describes.
    ///
    /// Raises:
    ///     AccessError: If a URL or DSN is invalid, a root cannot be
    ///         resolved, or an MCP entry lists a refused variable.
    pub fn build(&self) -> Result<Allowlists, AccessError> {
        let mut allow = Allowlists::new()
            .scripted_provider(self.scripted_provider)
            .exec_tools(self.exec_tools)
            .max_runs(self.max_runs.unwrap_or(linch_rpc::DEFAULT_MAX_RUNS))
            .max_connections((self.max_connections).unwrap_or(linch_rpc::DEFAULT_MAX_CONNECTIONS))
            .max_agents(self.max_agents.unwrap_or(linch_rpc::DEFAULT_MAX_AGENTS))
            .provider_base_urls(&self.provider_base_urls)?
            .workspace_roots(&self.workspace_roots)?
            .postgres_dsns(&self.postgres_dsns)?
            .mcp_commands(&self.mcp_commands)?
            .mcp_urls(&self.mcp_urls)?;
        if let Some(modes) = &self.permission_modes {
            allow = allow.permission_modes(modes);
        }
        Ok(allow)
    }
}

#[cfg(test)]
mod tests;
