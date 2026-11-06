//! Reloads: replace an agent's skills or subagents while it runs, each
//! reload a reversible registration.
//!
//! A reload changes what new sessions take into their frozen prefix and
//! what the `Skill` and `Subagent` tools run. A session that already has a
//! prefix keeps
//! it byte for byte and is told the change in a catalog notice at its
//! tail (see [`crate::catalog`]). The run contract stays the one the agent
//! was built with, so a run of the agent resumes across reloads.

use std::sync::{Arc, OnceLock};

use super::{Agent, AgentInner, BuildError};
use crate::catalog::LiveSkill;
use crate::message::SystemBlock;
use crate::registration::Registration;
use crate::skills::{Skill, SkillSet};
use crate::subagents::{Roster, SubagentDefinition, SubagentTool};

impl Agent {
    /// Replace the agent's skills with `skills`, the built-in `verify`
    /// skill joining them as at build.
    ///
    /// The `Skill` tool loads from the new set at once; a new session lists
    /// it in its system prompt; a session with a prefix is told what
    /// changed at the tail of its next logged user message.
    ///
    /// Args:
    ///     skills: The skills the agent has from now on.
    ///
    /// Returns:
    ///     A registration whose dispose brings back the skills the agent
    ///     had before, unless a later reload replaced them since, in which
    ///     case only this reload's set is dropped.
    ///
    /// Raises:
    ///     BuildError: `Invalid` when the agent was built without skills;
    ///         `Contract` if a skill cannot be digested.
    pub fn reload_skills(&self, skills: Vec<Skill>) -> Result<Registration, BuildError> {
        let versions = (self.inner.skills.as_ref())
            .ok_or_else(|| BuildError::Invalid("this agent has no skills".into()))?;
        Ok(versions.push(Arc::new(SkillSet::new(skills)?)))
    }

    /// Replace the subagents the `Subagent` tool runs with `definitions`.
    ///
    /// The tool's description lists the new roster, so a session with a
    /// prefix is told the tool was updated; its schema, and so the prefix,
    /// stays the same. A call, a child built with [`Agent::subagent`] and a
    /// reattached background worker all take the new roster.
    ///
    /// Args:
    ///     definitions: The subagents the agent has from now on.
    ///
    /// Returns:
    ///     A registration whose dispose brings back the roster and tool the
    ///     agent had before, unless a later reload replaced them since.
    ///
    /// Raises:
    ///     BuildError: `Invalid` when the agent was built without subagents.
    pub fn reload_subagents(
        &self,
        definitions: Vec<SubagentDefinition>,
    ) -> Result<Registration, BuildError> {
        let versions = (self.inner.subagents.as_ref())
            .ok_or_else(|| BuildError::Invalid("this agent has no subagents".into()))?;
        let roster = Arc::new(Roster::new(definitions));
        let handle = Arc::new(OnceLock::from(Arc::downgrade(&self.inner)));
        let tool = SubagentTool::new(Arc::clone(&roster), handle, self.inner.workers);
        let tool = (self.inner.tools.replace(Arc::new(tool))).map_err(|error| {
            BuildError::Invalid(format!("the Subagent tool cannot be replaced: {error}"))
        })?;
        let roster = versions.push(roster);
        Ok(Registration::new(move || {
            tool.dispose();
            roster.dispose();
        }))
    }
}

impl AgentInner {
    /// The system blocks a new session's prefix takes now: the static
    /// blocks with the current skill listing in its place.
    pub(crate) fn system(&self) -> Vec<SystemBlock> {
        let mut system = self.system.clone();
        if let (Some(at), Some(set)) = (self.listing_at, self.skill_set()) {
            system.splice(at..at, set.listing.clone());
        }
        system
    }

    /// The current skill set; `None` when skills are off.
    pub(crate) fn skill_set(&self) -> Option<Arc<SkillSet>> {
        self.skills.as_ref().map(|versions| versions.current())
    }

    /// The skills the agent's listing shows now; none when it lists none.
    pub(crate) fn listed_skills(&self) -> Vec<LiveSkill> {
        match (self.listing_at, self.skill_set()) {
            (Some(_), Some(set)) => set.listed.clone(),
            _ => Vec::new(),
        }
    }
}
