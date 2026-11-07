//! [`DeepAgentProfile`]: a named bundle of the deep agent's durability,
//! coordinator mode, limits and worker policy, and the three built-in ones.

use linch_core::Workers;
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// A named deep-agent policy bundle, so a deep agent's authority, worker
/// retention and spending are visible and reviewable. A host can still
/// override `durable` and `coordinator` (see [`super::DeepAgent`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeepAgentProfile {
    /// The profile's name.
    pub name: String,
    /// Whether the agent must keep its runs and sessions in durable stores.
    pub durable: bool,
    /// Whether the agent runs as a coordinator (see
    /// [`super::COORDINATOR_SYSTEM_PROMPT`]).
    pub coordinator: bool,
    /// Most provider calls one run may make, unless the spec sets its own;
    /// `None` sets no limit.
    pub max_turns: Option<u32>,
    /// Python's token budget across the whole subagent tree. Kept so the
    /// profiles read as in Python, but nothing enforces it yet: the kernel
    /// has no run budget.
    pub max_tokens: Option<u64>,
    /// Whether a foreground worker stays addressable for
    /// `SubagentContinue` once it answered.
    pub retain_subagents: bool,
    /// Whether the `Subagent` tool may run a worker in the background.
    pub enable_background_subagents: bool,
}

/// The names of the built-in profiles, in Python's order.
pub const PROFILE_NAMES: [&str; 3] = ["balanced", "coordinator", "unbounded"];

impl DeepAgentProfile {
    /// The built-in profile `name`:
    /// - `balanced`: durable, 64 turns, 1,000,000 tokens, workers retained
    ///   and allowed in the background;
    /// - `coordinator`: the same as a coordinator;
    /// - `unbounded`: no turn or token limit, for a host that owns those
    ///   limits itself. A host must name it to get no cap.
    ///
    /// Raises:
    ///     DeepAgentError: `UnknownProfile` for any other name.
    pub fn named(name: &str) -> Result<Self, DeepAgentError> {
        let bounded = |name: &str, coordinator| Self {
            name: name.to_owned(),
            durable: true,
            coordinator,
            max_turns: Some(64),
            max_tokens: Some(1_000_000),
            retain_subagents: true,
            enable_background_subagents: true,
        };
        match name {
            "balanced" => Ok(bounded(name, false)),
            "coordinator" => Ok(bounded(name, true)),
            "unbounded" => Ok(Self {
                max_turns: None,
                max_tokens: None,
                ..bounded(name, false)
            }),
            _ => Err(DeepAgentError::UnknownProfile {
                name: name.to_owned(),
            }),
        }
    }

    /// The three built-in profiles, in Python's order.
    pub fn built_in() -> Vec<Self> {
        (PROFILE_NAMES.iter())
            .filter_map(|name| Self::named(name).ok())
            .collect()
    }

    /// How the `Subagent` tool keeps its workers under this profile.
    pub fn workers(&self) -> Workers {
        Workers {
            background: self.enable_background_subagents,
            retain: self.retain_subagents,
            ..Workers::default()
        }
    }
}

/// A deep agent could not be set up.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DeepAgentError {
    /// No built-in profile has this name.
    #[error("unknown deep-agent profile {name:?}; choose one of: balanced, coordinator, unbounded")]
    UnknownProfile {
        /// The name asked for.
        name: String,
    },
    /// The deep agent is durable but its stores are not.
    #[error(
        "a durable deep agent needs durable stores; use the Postgres stores, \
         or set durable to false for ephemeral use"
    )]
    NotDurable,
}
