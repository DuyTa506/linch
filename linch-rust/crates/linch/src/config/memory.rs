//! [`MemoryConfig`]: the long-term memory of an agent.

use linch_tools::MemoryScope;
use serde::{Deserialize, Serialize};

/// Gives the agent long-term memory: the `MemorySave`, `MemorySearch` and
/// `MemoryDelete` tools over a store the open picks from the stores
/// config, and optionally an automatic search before each request.
///
/// The store is Postgres (`linch_memories`, found with full-text search)
/// with Postgres stores, so memory outlives the process and every worker of
/// the namespace sees it, and in-process memory otherwise (for tests and
/// development). The tools need no host callback, so a scheduled or
/// background agent has them too. The namespace and the scope are the
/// host's: the model never names either.
///
/// `MemorySave` and `MemoryDelete` are `write` tools, so a background run
/// (which cannot answer an approval) needs a permission mode that allows
/// them, such as `accept_edits`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryConfig {
    /// Whose memories the agent reads and writes: `agent` (shared by every
    /// session of the namespace) when absent, or `principal` (one memory
    /// per tenant and user; a run with no principal has none).
    #[serde(default, skip_serializing_if = "is_agent")]
    pub scope: MemoryScope,
    /// Search memory with the user's latest message before each request and
    /// show the model what it finds. Off when absent. It joins the run
    /// contract only when on.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recall: Option<RecallConfig>,
    /// The most memories the agent's tenant may keep in all its namespaces;
    /// a save past it fails with `limit_reached`. No cap when absent. A
    /// server's tenant allowlist bounds it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_memories: Option<u64>,
}

fn is_agent(scope: &MemoryScope) -> bool {
    *scope == MemoryScope::Agent
}

/// Automatic recall (see [`MemoryConfig::recall`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecallConfig {
    /// Most memories one request carries, 1 to 20; 5 when absent.
    #[serde(default = "default_k")]
    pub k: u32,
}

fn default_k() -> u32 {
    5
}

impl Default for RecallConfig {
    fn default() -> Self {
        Self { k: default_k() }
    }
}
