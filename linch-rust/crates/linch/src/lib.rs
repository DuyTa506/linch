//! The Rust facade of Linch: the public API of the kernel crates in one
//! place, and [`AgentConfig`], one serde value that opens a running
//! [`Agent`].
//!
//! Everything `linch_core` exports at its root is re-exported here, so a
//! host writes `linch::Agent`, `linch::RunOptions`, `linch::Envelope`. The
//! adapter crates are re-exported whole as [`providers`], [`tools`] and
//! [`postgres`].
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
//! let agent = config.open().await?;
//! let mut run = agent.run("List the Rust files.", linch::RunOptions::new("s1"));
//! while let Some(envelope) = run.next().await {
//!     println!("{}", envelope?.event.to_value()?);
//! }
//! # Ok(()) }
//! ```
//!
//! [`AgentConfig`] covers the declarative tier: the [`AgentSpec`], the
//! provider by id plus its options, the stores (memory or a Postgres DSN),
//! the built-in workspace tools from [`tools`], and the permission policy.
//! Provider keys are never part of it; [`AgentConfig::open`] reads each one
//! from the process environment. Anything the config cannot express (custom
//! tools, hooks, other stores) is bound in code with [`AgentBuilder`].

#![forbid(unsafe_code)]

mod config;
mod error;
mod open;

pub use config::{AgentConfig, ProviderConfig, ReasoningSummaryConfig, StoreConfig};
pub use error::OpenError;
pub use linch_core::*;

/// The HTTP model provider adapters (`linch-providers`).
pub use linch_providers as providers;

/// The Postgres run and session stores (`linch-postgres`).
pub use linch_postgres as postgres;

/// The workspace file tools and Bash (`linch-tools`).
pub use linch_tools as tools;
