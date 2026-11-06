//! [`DeepAgentConfig`]: the deep agent preset of an agent.

use linch_ext::deep_agent::{DeepAgent, DeepAgentError, DeepAgentProfile};
use serde::{Deserialize, Serialize};

/// Turns an agent into a deep agent (see [`linch_ext::deep_agent`]): a
/// model that plans with the session task tools, delegates to the
/// `researcher`, `planner`, `implementer` and `verification` subagents,
/// keeps its workers, and keeps notes in the virtual filesystem and file
/// memory.
///
/// Opening it turns on, on top of what the config sets:
/// - `tasks`;
/// - the `Skill` tool, with the built-in skills (the config directory's
///   only when `skills` is on);
/// - the `Subagent` tool, with the deep agent's roster (and the config
///   directory's subagents when `subagents` is on; a file there overrides
///   a roster entry of the same name);
/// - `workers` from the profile, unless the config sets them;
/// - `filesystem` with no offload, unless the config sets it, and the
///   file memory hook on it ([`linch_ext::deep_agent::FileMemoryContext`]);
/// - the preset's instructions before `spec.system_prompt`, and the
///   profile's `max_turns` unless the spec sets one.
///
/// It never adds a workspace: without one the agent has no workspace
/// tools, and the roster's definitions name tools it does not have.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeepAgentConfig {
    /// The profile; `balanced` when absent.
    #[serde(default)]
    pub profile: DeepAgentProfileChoice,
    /// Overrides the profile's `durable`. A durable deep agent needs the
    /// Postgres stores, or the open fails.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub durable: Option<bool>,
    /// Overrides the profile's `coordinator`. A coordinator gets the
    /// coordinator instructions; unlike in Python it keeps the workspace
    /// tools, which the kernel cannot hide from a parent alone yet.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub coordinator: Option<bool>,
}

/// A deep-agent profile: a built-in one by name (`balanced`,
/// `coordinator` or `unbounded`), or one written out.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum DeepAgentProfileChoice {
    /// The built-in profile of this name.
    Named(String),
    /// A profile written out in full.
    Custom(DeepAgentProfile),
}

impl Default for DeepAgentProfileChoice {
    fn default() -> Self {
        Self::Named("balanced".into())
    }
}

impl DeepAgentConfig {
    /// The deep agent this config describes: its profile with the
    /// overrides applied.
    ///
    /// Raises:
    ///     DeepAgentError: `UnknownProfile` for a name no built-in has.
    pub fn resolve(&self) -> Result<DeepAgent, DeepAgentError> {
        let mut deep = match &self.profile {
            DeepAgentProfileChoice::Named(name) => DeepAgent::named(name)?,
            DeepAgentProfileChoice::Custom(profile) => DeepAgent::new(profile.clone()),
        };
        deep.durable = self.durable.unwrap_or(deep.durable);
        deep.coordinator = self.coordinator.unwrap_or(deep.coordinator);
        Ok(deep)
    }
}
