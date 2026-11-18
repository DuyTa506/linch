//! [`ServerConfig`]: the listeners, the tenants with their bearer tokens,
//! and each tenant's allowlists, as one strict JSON value.

use std::collections::BTreeMap;
use std::fmt;
use std::net::SocketAddr;
use std::num::NonZeroU32;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use linch::{ExecutionConfig, PermissionMode, SystemSection};
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
    /// Run a worker in this process: it fires the schedules and runs the
    /// queued jobs of the tenants' namespaces (and of `local_namespaces`)
    /// in one database. Absent: no worker, no scheduler loop, no query.
    #[serde(default)]
    pub worker: Option<WorkerSection>,
}

/// The `worker` section: where the worker's queue is and how it runs.
/// Each field but `dsn` takes [`linch_worker::WorkerConfig`]'s default
/// when absent.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerSection {
    /// The Postgres DSN of the database (and `search_path` schema) whose
    /// queue this worker serves, inline or from the environment: the one
    /// the tenants' agents store in. Background runs store through it, not
    /// through the DSN of the agent that scheduled them.
    pub dsn: TokenSource,
    /// The owner name of its job holds and run claims; unique per worker.
    /// `linch-server-<pid>-<start time>` when absent.
    #[serde(default)]
    pub owner: Option<String>,
    /// The queues it serves, one job from each in turn; `["default"]`.
    #[serde(default)]
    pub queues: Option<Vec<String>>,
    /// The most jobs it runs at once; 32.
    #[serde(default)]
    pub max_jobs: Option<usize>,
    /// The size of its own Postgres pool (the queue and the run records;
    /// each run's agent uses its tenant's pool); the default pool size.
    #[serde(default)]
    pub max_connections: Option<u32>,
    /// The longest wait between polls, and between two checks for due
    /// schedules; 1000.
    #[serde(default)]
    pub poll_interval_ms: Option<u64>,
    /// How long a taken job stays hidden without a heartbeat; 30000. Keep
    /// the agents' `stores.lease_ttl_ms` no longer than this.
    #[serde(default)]
    pub visibility_timeout_ms: Option<u64>,
    /// The wait before a job's second attempt, doubling each time; 5000.
    #[serde(default)]
    pub retry_delay_ms: Option<u64>,
    /// How long ended jobs stay in the history, and failed jobs kept;
    /// 7 days.
    #[serde(default)]
    pub history_retention_ms: Option<u64>,
    /// Namespace roots outside every tenant whose jobs it also runs,
    /// without checks (those a stdio client scheduled): a root `dev`
    /// covers `dev` and `dev/...`.
    #[serde(default)]
    pub local_namespaces: Vec<String>,
}

/// One tenant: the tokens that name it and what its agents may use.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TenantConfig {
    /// Bearer tokens, each given inline or by environment variable.
    /// None for a tenant that only a worker serves.
    #[serde(default)]
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
    /// Whether a workspace agent gets `Bash`, which runs commands through
    /// its execution backend (unconfined, as the server's user, with
    /// `local`), with `BashOutput` and `BashKill`; off when absent.
    pub exec_tools: bool,
    /// Whether a config may set `spec.tool_cache`, which keeps tool results
    /// in the server's memory or database, and so only with
    /// `spec.redaction` over tool results; off when absent. The cache
    /// serves a result to the session that stored it unless
    /// `tool_cache_shared` allows `scope: "namespace"`, and every
    /// namespace of the tenant together holds at most
    /// `max_tool_cache_bytes`.
    pub tool_cache: bool,
    /// Whether a tenant's `tool_cache` may share results across the
    /// sessions of its namespace (`scope: "namespace"`); off when absent.
    pub tool_cache_shared: bool,
    /// Bytes of cached results all the tenant's namespaces may hold
    /// together, at least 1; [`linch_rpc::DEFAULT_TOOL_CACHE_BYTES`]
    /// (64 MiB) when absent.
    pub max_tool_cache_bytes: Option<u64>,
    /// Runs the tenant's connections may drive at once, together, at
    /// least 1; [`linch_rpc::DEFAULT_MAX_RUNS`] when absent.
    pub max_runs: Option<usize>,
    /// Connections that may act for the tenant at once, at least 1;
    /// [`linch_rpc::DEFAULT_MAX_CONNECTIONS`] when absent.
    pub max_connections: Option<usize>,
    /// Agents the tenant's connections may keep open at once, together,
    /// at least 1; [`linch_rpc::DEFAULT_MAX_AGENTS`] when absent.
    pub max_agents: Option<usize>,
    /// The largest `stores.max_connections` a config may ask for, and so
    /// the most connections of each of the tenant's own Postgres pools, at
    /// least 1; the server's default pool size
    /// ([`linch::postgres::default_max_connections`]) when absent.
    pub max_pool_connections: Option<u32>,
    /// The most provider calls the tenant's runs make at once on each
    /// provider key and endpoint, at least 1; absent, unlimited. A config
    /// may ask for less and gets this when it asks for nothing. It holds
    /// within the server process, not across processes.
    pub max_provider_concurrency: Option<u32>,
    /// Schedules the tenant may keep in each database, at least 1; absent,
    /// it may create none.
    pub max_schedules: Option<u64>,
    /// The shortest time between two fires of its schedules, in
    /// milliseconds; [`linch_rpc::DEFAULT_MIN_SCHEDULE_INTERVAL`] (60 s)
    /// when absent.
    pub min_schedule_interval_ms: Option<u64>,
    /// Background runs (`job.enqueue`) it may have unfinished at once in
    /// each database, at least 1; absent, it may queue none.
    pub max_queued_jobs: Option<u64>,
    /// Whether a config may turn on long-term `memory`; off when absent.
    /// It needs `max_memories`: the server refuses to load a config that
    /// allows memory without a cap.
    pub memory: bool,
    /// Memories the tenant may keep in each database, in all its
    /// namespaces, at least 1; required with `memory`.
    pub max_memories: Option<u64>,
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
    /// Execution backends a config may choose, each with its whole policy
    /// (`{"backend": "sandbox", ...}`); a config naming none gets the
    /// first. `[{"backend": "local"}]` when absent; list only a sandbox to
    /// hold the tenant to it.
    pub execution: Option<Vec<ExecutionConfig>>,
    /// System prompt sections the operator adds to every agent of the
    /// tenant, each `{"name", "text", "order"?, "cacheable"?}`; a tenant
    /// cannot remove or rename them. None when absent.
    pub system_sections: Vec<SystemSection>,
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
    /// Neither `socket` nor `http` is set, and there is no `worker`.
    #[error("the config sets neither socket nor http nor worker")]
    NoListener,
    /// No tenant has a token, so every connection would be refused.
    #[error("the config gives no tenant a token")]
    NoToken,
    /// A tenant's `max_runs`, `max_connections`, `max_agents`,
    /// `max_pool_connections`, `max_schedules` or `max_queued_jobs` is 0.
    #[error(
        "tenant {tenant:?}: {field} is 0, which lets it do nothing; \
         set at least 1, or leave it out for the default"
    )]
    ZeroCap {
        /// The tenant.
        tenant: String,
        /// `max_runs`, `max_connections`, `max_agents` or
        /// `max_pool_connections`.
        field: &'static str,
    },
    /// A tenant allows `memory` without a `max_memories` cap, which would
    /// let a model fill its tables.
    #[error(
        "tenant {tenant:?}: allow.memory needs allow.max_memories, \
         a cap on the memories it may keep"
    )]
    MemoryNeedsCap {
        /// The tenant.
        tenant: String,
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
    ///     ConfigError: If the JSON is invalid, sets no listener, gives
    ///         a tenant a cap of 0, or allows `memory` without
    ///         `max_memories`.
    pub fn from_json(text: &str) -> Result<Self, ConfigError> {
        let config: Self = serde_json::from_str(text).map_err(ConfigError::Json)?;
        if config.socket.is_none() && config.http.is_none() && config.worker.is_none() {
            return Err(ConfigError::NoListener);
        }
        for (name, tenant) in &config.tenants {
            let allow = &tenant.allow;
            let pool = allow.max_pool_connections.map(|max| max as usize);
            let caps = [
                ("max_runs", allow.max_runs),
                ("max_connections", allow.max_connections),
                ("max_agents", allow.max_agents),
                ("max_pool_connections", pool),
                (
                    "max_provider_concurrency",
                    allow.max_provider_concurrency.map(|max| max as usize),
                ),
                ("max_schedules", allow.max_schedules.map(|max| max as usize)),
                (
                    "max_queued_jobs",
                    allow.max_queued_jobs.map(|max| max as usize),
                ),
                (
                    "max_tool_cache_bytes",
                    allow.max_tool_cache_bytes.map(|max| max as usize),
                ),
                ("max_memories", allow.max_memories.map(|max| max as usize)),
            ];
            if let Some((field, _)) = caps.into_iter().find(|(_, cap)| *cap == Some(0)) {
                let tenant = name.clone();
                return Err(ConfigError::ZeroCap { tenant, field });
            }
            if allow.memory && allow.max_memories.is_none() {
                let tenant = name.clone();
                return Err(ConfigError::MemoryNeedsCap { tenant });
            }
        }
        Ok(config)
    }

    /// Build every tenant and its tokens, reading `{"env": ...}` tokens
    /// with `env`; [`Self::build`] without the tenants.
    ///
    /// Raises:
    ///     ConfigError: As [`Self::build`].
    pub fn tokens(&self, env: impl Fn(&str) -> Option<String>) -> Result<Tokens, ConfigError> {
        Ok(self.build(env)?.tokens)
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
    ///     The token table the listeners check every connection against,
    ///     and the tenants it names, which a worker shares.
    ///
    /// Raises:
    ///     ConfigError: If a variable is unset, a tenant, token or
    ///         allowlist entry is invalid, or the config has a listener
    ///         and no tenant has a token.
    pub fn build(&self, env: impl Fn(&str) -> Option<String>) -> Result<Built, ConfigError> {
        let mut tokens = Tokens::new();
        let mut tenants = Vec::new();
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
            tenants.push(built);
        }
        let listens = self.socket.is_some() || self.http.is_some();
        if listens && tokens.is_empty() {
            return Err(ConfigError::NoToken);
        }
        Ok(Built { tokens, tenants })
    }
}

/// What [`ServerConfig::build`] makes: the token table and the tenants.
#[derive(Debug)]
pub struct Built {
    /// The tokens the listeners check.
    pub tokens: Tokens,
    /// Every tenant, each shared by its tokens.
    pub tenants: Vec<Arc<Tenant>>,
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
            .tool_cache(self.tool_cache)
            .tool_cache_shared(self.tool_cache_shared)
            .tool_cache_bytes(
                (self.max_tool_cache_bytes).unwrap_or(linch_rpc::DEFAULT_TOOL_CACHE_BYTES),
            )
            .max_runs(self.max_runs.unwrap_or(linch_rpc::DEFAULT_MAX_RUNS))
            .max_connections((self.max_connections).unwrap_or(linch_rpc::DEFAULT_MAX_CONNECTIONS))
            .max_agents(self.max_agents.unwrap_or(linch_rpc::DEFAULT_MAX_AGENTS))
            .max_pool_connections(
                (self.max_pool_connections.and_then(NonZeroU32::new))
                    .unwrap_or_else(linch::postgres::default_max_connections),
            )
            .max_provider_concurrency(self.max_provider_concurrency.and_then(NonZeroU32::new))
            .provider_base_urls(&self.provider_base_urls)?
            .workspace_roots(&self.workspace_roots)?
            .postgres_dsns(&self.postgres_dsns)?
            .mcp_commands(&self.mcp_commands)?
            .mcp_urls(&self.mcp_urls)?
            .max_schedules(self.max_schedules)
            .max_queued_jobs(self.max_queued_jobs)
            .system_sections(&self.system_sections)?
            .memory(self.memory)
            .max_memories(self.max_memories);
        if let Some(ms) = self.min_schedule_interval_ms {
            allow = allow.min_schedule_interval(Duration::from_millis(ms));
        }
        if let Some(modes) = &self.permission_modes {
            allow = allow.permission_modes(modes);
        }
        if let Some(backends) = &self.execution {
            allow = allow.execution(backends);
        }
        Ok(allow)
    }
}

#[cfg(test)]
mod tests;
