//! [`OpenError`]: why an [`AgentConfig`](crate::AgentConfig) did not open.

use linch_core::{BuildError, PolicyError, ProviderError, RegistryError};
use linch_ext::deep_agent::DeepAgentError;
use linch_mcp::McpError;
use linch_tools::{ExecutionError, FsError};
use thiserror::Error;

use crate::profile::ProfileError;

/// [`AgentConfig::open`](crate::AgentConfig::open) failed. No message ever
/// holds a provider key or a DSN.
#[derive(Debug, Error)]
pub enum OpenError {
    /// The provider's key variable is unset or blank in the environment.
    #[error("provider {provider} needs its key in the environment variable {env}")]
    MissingKey {
        /// Provider id.
        provider: &'static str,
        /// The environment variable that must hold the key.
        env: &'static str,
    },
    /// The provider client could not be built.
    #[error("provider could not be built: {0}")]
    Provider(#[source] ProviderError),
    /// The workspace root is missing or not a directory.
    #[error("workspace unusable: {0}")]
    Workspace(#[source] FsError),
    /// The execution backend cannot confine what its policy says on this
    /// host (no Landlock, no `prlimit`, no `docker`), or its policy is
    /// invalid. The open fails rather than run `Bash` unconfined.
    #[error("execution backend unusable: {0}")]
    Execution(#[source] ExecutionError),
    /// A built-in tool could not be registered.
    #[error("tool registration failed: {0}")]
    Tools(#[from] RegistryError),
    /// The permission policy is malformed.
    #[error("permission policy rejected: {0}")]
    Policy(#[from] PolicyError),
    /// Connecting to Postgres or applying the migrations failed.
    #[error("Postgres stores could not be opened: {0}")]
    Store(#[source] Box<dyn std::error::Error + Send + Sync>),
    /// The database holds a migration this binary does not embed: a newer
    /// release migrated it. Nothing was read or written; upgrade this
    /// binary.
    #[error(
        "the database schema is newer than this binary: {table} holds migration {applied}, \
         the newest this binary knows is {latest}; upgrade this binary"
    )]
    SchemaNewer {
        /// The migration table that holds it.
        table: String,
        /// The newest applied migration.
        applied: i64,
        /// The newest migration this binary embeds.
        latest: i64,
    },
    /// An MCP server's config is invalid or its first connect failed. The
    /// servers connected before it are disconnected again.
    #[error("MCP server unusable: {0}")]
    Mcp(#[source] McpError),
    /// The deep agent preset names an unknown profile, or is durable on
    /// memory stores.
    #[error("deep agent unusable: {0}")]
    DeepAgent(#[from] DeepAgentError),
    /// The domain profile is invalid.
    #[error("profile unusable: {0}")]
    Profile(#[from] ProfileError),
    /// `schedule_tools` is on, but the stores are not Postgres or the host
    /// did not bind the schedule tools before the open.
    #[error("schedule_tools unusable: {0}")]
    ScheduleTools(&'static str),
    /// The `memory` config is invalid.
    #[error("memory unusable: {0}")]
    Memory(&'static str),
    /// `task_ledger` is set without `tasks`, or names a blank list or a
    /// budget under 200 characters.
    #[error("task_ledger unusable: {0}")]
    TaskLedger(&'static str),
    /// The agent itself could not be built.
    #[error(transparent)]
    Build(#[from] BuildError),
}
