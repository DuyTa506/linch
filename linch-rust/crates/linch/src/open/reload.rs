//! Reloading an opened agent's skills and subagents from its config
//! directory.

use linch_core::{BuildError, Registration};

use super::OpenedAgent;
use crate::deep;
use crate::project::{self, Skipped};

impl OpenedAgent {
    /// Scan the config directory's skills again, off the async loop, and
    /// make them the agent's (see [`linch_core::Agent::reload_skills`]). A
    /// session that has a prefix keeps it and is told what changed; a new
    /// session lists the new skills.
    ///
    /// As at open, a skill named like one of the agent's tools is skipped.
    ///
    /// Returns:
    ///     The registration whose dispose brings back the skills the agent
    ///     had before, and the skill directories the scan left out.
    ///
    /// Raises:
    ///     BuildError: `Invalid` when the agent was opened without skills.
    pub async fn reload_skills(&self) -> Result<(Registration, Vec<Skipped>), BuildError> {
        let mut loaded = project::load(self.config_dir.clone(), true, false, &self.tools).await;
        deep::add_roster(self.deep.as_ref(), &mut loaded);
        let registration = self
            .agent
            .reload_skills(loaded.skills.unwrap_or_default())?;
        Ok((registration, loaded.skipped))
    }

    /// Scan the config directory's subagents again, off the async loop,
    /// and make them the agent's (see
    /// [`linch_core::Agent::reload_subagents`]).
    ///
    /// Returns:
    ///     The registration whose dispose brings back the subagents the
    ///     agent had before, and the subagent files the scan left out.
    ///
    /// Raises:
    ///     BuildError: `Invalid` when the agent was opened without
    ///         subagents.
    pub async fn reload_subagents(&self) -> Result<(Registration, Vec<Skipped>), BuildError> {
        let mut loaded = project::load(self.config_dir.clone(), false, true, &self.tools).await;
        deep::add_roster(self.deep.as_ref(), &mut loaded);
        let definitions = loaded.subagents.unwrap_or_default();
        let registration = self.agent.reload_subagents(definitions)?;
        Ok((registration, loaded.skipped))
    }
}
