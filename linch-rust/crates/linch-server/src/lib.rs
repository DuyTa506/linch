//! The Linch sidecar's transports beyond stdio: a Unix socket and HTTP
//! with WebSocket, with bearer tokens, tenants and allowlists from a
//! [`ServerConfig`]. The `linch-server` binary serves stdio by default and
//! these with `--config FILE`; a custom server binary can serve them the
//! same way:
//!
//! ```no_run
//! # async fn run(text: &str) -> Result<(), Box<dyn std::error::Error>> {
//! use linch_server::{Listeners, ServerConfig};
//!
//! let config = ServerConfig::from_json(text)?;
//! let tokens = config.tokens(|name| std::env::var(name).ok())?;
//! let listeners = Listeners::bind(&config, tokens).await?;
//! let stop = tokio_util::sync::CancellationToken::new();
//! listeners.serve(stop).await;
//! # Ok(()) }
//! ```
//!
//! # Config
//!
//! One JSON object; unknown fields are refused at every level:
//!
//! ```json
//! {"socket": "/run/linch/linch.sock",
//!  "http": "127.0.0.1:8731",
//!  "tenants": {
//!    "acme": {
//!      "tokens": [{"env": "LINCH_TOKEN_ACME"}, {"value": "a-long-random-token"}],
//!      "allow": {
//!        "scripted_provider": false,
//!        "exec_tools": false,
//!        "max_runs": 16,
//!        "max_agents": 16,
//!        "max_connections": 8,
//!        "provider_base_urls": ["http://vllm.internal:8000/v1"],
//!        "permission_modes": ["default", "accept_edits"],
//!        "workspace_roots": ["/srv/linch/acme"],
//!        "postgres_dsns": ["postgres://linch_acme@db.internal:5432/linch"],
//!        "mcp_commands": [{"command": "/usr/local/bin/git-mcp",
//!                          "args": ["--read-only"], "env": ["GIT_TOKEN"]}],
//!        "mcp_urls": ["https://mcp.internal"],
//!        "execution": [{"backend": "sandbox", "network": false}]}}}}
//! ```
//!
//! At least one of `socket`, `http` and `worker` is required, and with a
//! listener at least one token. Each token names its tenant: a token is given inline or as the
//! name of an environment variable read at startup. Every `allow` list
//! is empty when absent (it allows nothing), except `permission_modes`,
//! which defaults to `default` and `accept_edits`; `scripted_provider`,
//! `provider_info` (`provider.capabilities`, `provider.models` and
//! `provider.model`, which send the key to an allowed `base_url`),
//! `exec_tools` (`Bash`, `BashOutput` and `BashKill` for workspace
//! agents), `tool_cache` (`spec.tool_cache`, which also needs
//! `spec.redaction` over tool results) and `tool_cache_shared` (its
//! `scope: "namespace"`) are off when absent; `max_tool_cache_bytes`
//! (64 MiB when absent) bounds the cached results of all the tenant's
//! namespaces together;
//! `max_runs`, `max_agents` and `max_connections`, the tenant's caps
//! across all its connections, default to 16, 16 and 8;
//! `max_pool_connections`, the largest `stores.max_connections` it may
//! ask for, defaults to the server's default pool size (the machine's
//! cores, at least 10 and at most 32). `max_schedules` and
//! `max_queued_jobs` give the tenant schedules and background runs (none
//! when absent), and `min_schedule_interval_ms` (60000 when absent) is the
//! shortest time between two fires. `execution`, the backends a config
//! may run `Bash` through (a config naming none gets the first), defaults
//! to `[{"backend": "local"}]`. A 0 is refused when the config
//! loads. See [`linch_rpc::Allowlists`] for how
//! each entry matches.
//!
//! # Worker
//!
//! A `worker` section runs [`BackgroundWorker`] in the process: it fires
//! the schedules and runs the queued jobs of every tenant's namespaces
//! (and of its `local_namespaces`) in the database its `dsn` names, opening
//! each job's stored agent config with [`linch_rpc::BackgroundAgents`].
//! Any number of servers may run one on the same database; each fire and
//! each job runs once. Without the section the server polls nothing.
//!
//! ```json
//! {"worker": {"dsn": {"env": "LINCH_WORKER_DSN"}, "max_jobs": 8,
//!             "queues": ["default"], "poll_interval_ms": 1000,
//!             "visibility_timeout_ms": 30000, "local_namespaces": ["dev"]},
//!  "tenants": {"acme": {"allow": {"max_schedules": 10, "max_queued_jobs": 100}}}}
//! ```
//!
//! # Transports
//!
//! - **Socket:** newline-delimited JSON-RPC as on stdio. The first line
//!   must be `connection.auth {token}`; anything else closes the
//!   connection. A socket file left by a stopped server is replaced and
//!   the file is removed at shutdown. Its permissions follow the umask.
//! - **HTTP:** `GET /rpc` with `Authorization: Bearer <token>` upgrades
//!   to a WebSocket carrying one JSON-RPC message per text frame; the
//!   `101` names the token's tenant in `Linch-Tenant: <name>`. A
//!   missing or unknown token gets `401` with `WWW-Authenticate: Bearer`;
//!   a tenant already holding its `max_connections` gets `429`, both
//!   before the upgrade. Until it is upgraded a connection has
//!   [`HEADER_READ_TIMEOUT`] for each request head (then it is closed
//!   without an answer), and at most [`MAX_PENDING_UPGRADES`] wait at
//!   once across the server; one more is closed unread.
//!   The server speaks plain HTTP: put TLS in front of it, or listen on
//!   loopback only.
//!
//! Every connection is its own session of the protocol: its agent handles
//! and runs are its own, and it acts for its token's tenant. A tenant's
//! agents, across all its connections, share one Postgres pool per target;
//! two tenants never share one.

#![forbid(unsafe_code)]

mod config;
mod http;
mod listen;
mod liveness;
mod logging;
mod worker;

pub use config::{
    AllowConfig, Built, ConfigError, ServerConfig, TenantConfig, TokenSource, WorkerSection,
};
pub use http::{HEADER_READ_TIMEOUT, MAX_PENDING_UPGRADES, PATH};
pub use listen::Listeners;
pub use liveness::LivenessConfig;
pub use logging::{init_logging, log_filter};
pub use worker::{BackgroundWorker, WorkerStartError};
