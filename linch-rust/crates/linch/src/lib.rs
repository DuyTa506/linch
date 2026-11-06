//! The Rust facade of Linch: the public API of the kernel crates in one
//! place, and [`AgentConfig`], one serde value that opens a running
//! [`Agent`].
//!
//! Everything `linch_core` exports at its root is re-exported here, so a
//! host writes `linch::Agent`, `linch::RunOptions`, `linch::Envelope`. The
//! adapter crates are re-exported whole as [`providers`], [`tools`],
//! [`postgres`] and [`mcp`].
//!
//! ```no_run
//! # async fn demo() -> Result<(), Box<dyn std::error::Error>> {
//! use futures::StreamExt;
//!
//! let config: linch::AgentConfig = serde_json::from_str(r#"{
//!     "namespace": "demo",
//!     "spec": {"model": "deepseek-flash"},
//!     "provider": {"id": "deepseek"},
//!     "workspace": "/srv/project"
//! }"#)?;
//! let opened = config.open().await?;
//! let mut run = opened
//!     .agent()
//!     .run("List the Rust files.", linch::RunOptions::new("s1"));
//! while let Some(envelope) = run.next().await {
//!     println!("{}", envelope?.event.to_value()?);
//! }
//! opened.close().await;
//! # Ok(()) }
//! ```
//!
//! [`AgentConfig`] covers the declarative tier: the [`AgentSpec`], the
//! provider by id plus its options, the stores (memory or a Postgres DSN),
//! the built-in workspace tools from [`tools`], the permission policy, MCP
//! servers ([`McpConfig`]), the virtual filesystem ([`FilesystemConfig`]),
//! and whether the agent loads the skills and
//! subagents of its config directory ([`project`]). Provider keys are
//! never part of it;
//! [`AgentConfig::open`] reads each one from the process environment.
//! Custom tools and hooks are bound in code and passed to
//! [`AgentConfig::open_with`], along with the [`PgPools`] that lets agents
//! on one DSN share a Postgres pool; anything else (other stores) is bound
//! with [`AgentBuilder`]. The result is an [`OpenedAgent`], whose
//! [`close`](OpenedAgent::close) disconnects the MCP servers.

#![forbid(unsafe_code)]

mod config;
mod error;
mod files;
mod open;
mod pools;
pub mod project;

pub use config::{
    AgentConfig, FilesystemConfig, McpConfig, McpTransportConfig, ProviderConfig,
    ReasoningSummaryConfig, ReconnectConfig, StoreConfig,
};
pub use error::OpenError;
pub use linch_core::*;
pub use open::OpenedAgent;
pub use pools::PgPools;

/// The MCP client (`linch-mcp`).
pub use linch_mcp as mcp;

/// The HTTP model provider adapters (`linch-providers`).
pub use linch_providers as providers;

/// The Postgres run and session stores (`linch-postgres`).
pub use linch_postgres as postgres;

/// The workspace file tools and Bash (`linch-tools`).
pub use linch_tools as tools;
