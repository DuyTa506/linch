//! [`OpenError`]: why an [`AgentConfig`](crate::AgentConfig) did not open.

use linch_core::{BuildError, PolicyError, ProviderError, RegistryError};
use linch_tools::FsError;
use thiserror::Error;

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
    /// A built-in tool could not be registered.
    #[error("tool registration failed: {0}")]
    Tools(#[from] RegistryError),
    /// The permission policy is malformed.
    #[error("permission policy rejected: {0}")]
    Policy(#[from] PolicyError),
    /// Connecting to Postgres or applying the migrations failed.
    #[error("Postgres stores could not be opened: {0}")]
    Store(#[source] Box<dyn std::error::Error + Send + Sync>),
    /// The agent itself could not be built.
    #[error(transparent)]
    Build(#[from] BuildError),
}
