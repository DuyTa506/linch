//! The agent a subagent's child run runs with, derived from its parent's.

use std::sync::Arc;

use super::spec::{self, contract_digest};
use super::{AgentInner, BuildError};
use crate::message::SystemBlock;
use crate::registry::ToolSnapshot;
use crate::skills::SKILL_TOOL;
use crate::subagents::{SUBAGENT_TOOL, SubagentDefinition};

/// Which of the parent's tools a child run sees: never `Subagent` (a child
/// cannot spawn), and only the named ones when the definition names any.
#[derive(Debug, Clone)]
pub(crate) struct ChildTools {
    only: Option<Vec<String>>,
}

impl ChildTools {
    fn of(definition: &SubagentDefinition) -> Self {
        let named = definition.tools.clone();
        Self {
            only: named.filter(|names| !names.iter().any(|name| name == "*")),
        }
    }

    fn keeps(&self, name: &str) -> bool {
        name != SUBAGENT_TOOL
            && (self.only.as_ref()).is_none_or(|only| only.iter().any(|n| n == name))
    }

    pub(crate) fn apply(&self, snapshot: &ToolSnapshot) -> ToolSnapshot {
        snapshot.filtered(|name| self.keeps(name))
    }
}

impl AgentInner {
    /// The tools a run of this agent sees now: the registry's, through the
    /// child filter of a subagent's agent.
    pub(crate) fn snapshot(&self) -> Arc<ToolSnapshot> {
        let snapshot = self.tools.snapshot();
        match &self.filter {
            None => snapshot,
            Some(filter) => Arc::new(filter.apply(&snapshot)),
        }
    }

    /// The agent of a `definition` subagent of this agent: the same
    /// bindings and spec, the child's tools, the system prompt with the
    /// skill listing when the child has the `Skill` tool, then the
    /// definition's body, and a contract that also covers the definition.
    /// It offers no subagents, so its runs cannot spawn.
    ///
    /// Raises:
    ///     BuildError: If the definition or the tools cannot be digested.
    pub(crate) fn child(&self, definition: &SubagentDefinition) -> Result<Self, BuildError> {
        let filter = ChildTools::of(definition);
        let tools = filter.apply(&self.tools.snapshot());
        let mut system = spec::prompt_block(&self.spec);
        if tools.resolve(SKILL_TOOL).is_some() {
            system.extend(self.skill_listing.clone());
        }
        system.push(SystemBlock {
            text: format!("User-provided instructions:\n\n{}", definition.body),
            cacheable: true,
        });
        let mut features = self.features.clone();
        let value =
            serde_json::to_value(definition).map_err(|e| BuildError::Invalid(e.to_string()))?;
        features.insert("subagent".into(), value);
        let hooks = self.hooks.policy_digest();
        let contract = contract_digest(&self.spec, &tools.contract_tools(), hooks, &features)?;
        Ok(Self {
            spec: self.spec.clone(),
            provider: Arc::clone(&self.provider),
            tools: Arc::clone(&self.tools),
            filter: Some(filter),
            permissions: self.permissions.clone(),
            hooks: Arc::clone(&self.hooks),
            runs: Arc::clone(&self.runs),
            sessions: Arc::clone(&self.sessions),
            namespace: self.namespace.clone(),
            contract,
            system,
            features,
            skills: self.skills,
            skill_listing: self.skill_listing.clone(),
            subagents: None,
            child: true,
            session_close: Vec::new(),
            registrations: Vec::new(),
        })
    }
}
