//! The deep agent preset: Python Linch's `create_deep_agent` setup of a
//! model that plans with the task tools, delegates to a subagent roster,
//! keeps its workers for `SubagentContinue`, and keeps notes in the
//! virtual filesystem and file memory.
//!
//! A deep agent is an ordinary agent with these settings; the loop is
//! unchanged. The model decides which subagents to spawn, when to run
//! them in the background and when to continue one, so the orchestration
//! is not deterministic and nothing journals it; use a
//! [`workflow`](crate::workflow) when replaying the orchestration itself
//! is the requirement.
//!
//! [`DeepAgent`] holds a [`DeepAgentProfile`] (the built-ins are
//! `balanced`, `coordinator` and `unbounded`) with its two overrides, and
//! gives what the agent is built from:
//! - [`DeepAgent::spec`]: the spec with [`DEEP_AGENT_SYSTEM_PROMPT`] (or
//!   [`COORDINATOR_SYSTEM_PROMPT`]) before the host's prompt, and the
//!   profile's turn limit unless the spec sets one;
//! - [`DeepAgent::workers`]: background and retained workers, for
//!   [`AgentBuilder::workers`](linch_core::AgentBuilder::workers);
//! - [`DeepAgent::subagents`]: [`deep_agent_subagents`] with the host's
//!   definitions, for
//!   [`AgentBuilder::subagents`](linch_core::AgentBuilder::subagents);
//! - [`FileMemoryContext`]: the `context_build` hook that shows the model
//!   `/memories/MEMORY.md`.
//!
//! The agent also needs the session task tools, the virtual filesystem
//! tools and the `Skill` tool (`linch_tools::task_tools`,
//! `linch_tools::vfs::vfs_tools`,
//! [`AgentBuilder::skills`](linch_core::AgentBuilder::skills)). The `linch`
//! facade wires all of it from `AgentConfig.deep_agent`.
//!
//! Two parts of Python's preset have no kernel support yet:
//! - a coordinator keeps the workspace tools: the kernel offers a parent
//!   every tool its subagents can use, so
//!   [`COORDINATOR_EXCLUDED_TOOLS`] are only named here, and the
//!   coordinator prompt tells the model not to do that work itself;
//! - [`DeepAgentProfile::max_tokens`] is not enforced: there is no run
//!   budget.

mod memory;
mod profile;
mod prompts;
mod subagents;
#[cfg(test)]
mod tests;

use linch_core::{AgentSpec, SubagentDefinition, Workers};

pub use memory::{FileMemoryContext, MEMORY_INDEX, MEMORY_INDEX_LINES, MEMORY_INSTRUCTION};
pub use profile::{DeepAgentError, DeepAgentProfile, PROFILE_NAMES};
pub use prompts::{COORDINATOR_SYSTEM_PROMPT, DEEP_AGENT_SYSTEM_PROMPT};
pub use subagents::deep_agent_subagents;

/// The tools Python takes away from a coordinator so it delegates the
/// heavy work, while its workers keep them. Not applied yet (see the
/// module docs).
pub const COORDINATOR_EXCLUDED_TOOLS: [&str; 6] = ["Edit", "Write", "Bash", "Grep", "Glob", "Read"];

/// A deep agent's settings: a profile and the two values a host may
/// override.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeepAgent {
    /// The policy bundle the other settings come from.
    pub profile: DeepAgentProfile,
    /// Whether the agent must have durable stores; the profile's when not
    /// overridden.
    pub durable: bool,
    /// Whether the agent is a coordinator; the profile's when not
    /// overridden.
    pub coordinator: bool,
}

impl DeepAgent {
    /// The settings of `profile`, with its durability and coordinator mode.
    pub fn new(profile: DeepAgentProfile) -> Self {
        Self {
            durable: profile.durable,
            coordinator: profile.coordinator,
            profile,
        }
    }

    /// The settings of the built-in profile `name` (see
    /// [`DeepAgentProfile::named`]).
    ///
    /// Raises:
    ///     DeepAgentError: `UnknownProfile` when no built-in has that name.
    pub fn named(name: &str) -> Result<Self, DeepAgentError> {
        DeepAgentProfile::named(name).map(Self::new)
    }

    /// The operating instructions: [`COORDINATOR_SYSTEM_PROMPT`] for a
    /// coordinator, else [`DEEP_AGENT_SYSTEM_PROMPT`].
    pub fn system_prompt(&self) -> &'static str {
        match self.coordinator {
            true => COORDINATOR_SYSTEM_PROMPT,
            false => DEEP_AGENT_SYSTEM_PROMPT,
        }
    }

    /// `spec` for a deep agent: [`Self::system_prompt`], then a blank line
    /// and the host's prompt when it has one, and the profile's
    /// `max_turns` when the spec sets none. Python put the preset's section
    /// before the host's instructions too.
    pub fn spec(&self, mut spec: AgentSpec) -> AgentSpec {
        let prompt = self.system_prompt();
        spec.system_prompt = match spec.system_prompt.is_empty() {
            true => prompt.to_owned(),
            false => format!("{prompt}\n\n{}", spec.system_prompt),
        };
        spec.max_turns = spec.max_turns.or(self.profile.max_turns);
        spec
    }

    /// How the `Subagent` tool keeps its workers (see
    /// [`DeepAgentProfile::workers`]).
    pub fn workers(&self) -> Workers {
        self.profile.workers()
    }

    /// The subagents the agent offers: `project` (the host's, for example
    /// the facade's `.linch/agents`), then each of [`deep_agent_subagents`]
    /// whose name none of them has (case-insensitively), as Python's
    /// registry let a project file override a built-in.
    pub fn subagents(&self, project: Vec<SubagentDefinition>) -> Vec<SubagentDefinition> {
        let mut roster = project;
        for definition in deep_agent_subagents() {
            let taken = (roster.iter()).any(|d| d.name.eq_ignore_ascii_case(&definition.name));
            if !taken {
                roster.push(definition);
            }
        }
        roster
    }
}
