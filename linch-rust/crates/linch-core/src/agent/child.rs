//! The agent a subagent's child run runs with, derived from its parent's.

use std::sync::Arc;

use super::spec::{self, contract_digest};
use super::{Agent, AgentInner, BuildError, RunOptions};
use crate::ids::RunId;
use crate::message::SystemBlock;
use crate::registry::ToolSnapshot;
use crate::runner::{self, RunStream};
use crate::skills::SKILL_TOOL;
use crate::subagents::{CONTINUE_TOOL, STOP_TOOL, SUBAGENT_TOOL, SubagentDefinition};
use crate::tool_search::TOOL_SEARCH_TOOL;

/// Which of the parent's tools a child run sees: never `Subagent` (a child
/// cannot spawn), the worker tools (it has no workers) nor `tool_search`
/// (it is offered every tool), and only the named ones when the
/// definition names any.
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
        ![SUBAGENT_TOOL, CONTINUE_TOOL, STOP_TOOL, TOOL_SEARCH_TOOL].contains(&name)
            && (self.only.as_ref()).is_none_or(|only| only.iter().any(|n| n == name))
    }

    pub(crate) fn apply(&self, snapshot: &ToolSnapshot) -> ToolSnapshot {
        snapshot.filtered(|name| self.keeps(name))
    }
}

impl Agent {
    /// The agent of this agent's subagent `name`, for a host that runs a
    /// subagent itself (a workflow step, say): what the `Subagent` tool
    /// runs a child run with. `None`, blank, `general-purpose` or
    /// `_default` names the general-purpose agent; another name is looked
    /// up case-insensitively.
    ///
    /// The agent shares this agent's bindings and stores, sees the
    /// definition's tools without the subagent tools, cannot spawn, and has
    /// its own contract. Start its runs with [`Agent::run_child`] to record
    /// the parent; the host drives and ends them. Its
    /// [`Agent::close_session`] runs this agent's session-close callbacks.
    ///
    /// Raises:
    ///     BuildError: `Invalid` when this agent has no subagents or none
    ///         has that name; `Contract` if the definition cannot be
    ///         digested.
    pub fn subagent(&self, name: Option<&str>) -> Result<Agent, BuildError> {
        let roster = (self.inner.subagents.as_ref())
            .ok_or_else(|| BuildError::Invalid("this agent has no subagents".into()))?
            .current();
        let definition = roster.get(name).ok_or_else(|| {
            let name = name.unwrap_or_default().trim();
            BuildError::Invalid(format!("unknown subagent type: {name}"))
        })?;
        let inner = Arc::new(self.inner.child(definition)?);
        Ok(Agent { inner })
    }

    /// Start a new run like [`Agent::run`], recorded as a child run of
    /// `parent_run_id`. Nothing else ties it to that run: the caller
    /// drives the stream, and the parent does not wait for it or cancel it.
    pub fn run_child(
        &self,
        prompt: impl Into<String>,
        options: RunOptions,
        parent_run_id: RunId,
    ) -> RunStream {
        let agent = Arc::clone(&self.inner);
        runner::start_child(agent, prompt.into(), options, parent_run_id)
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
        let listing_at = (tools.resolve(SKILL_TOOL).is_some()).then_some(system.len());
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
            listing_at,
            features,
            skills: self.skills.clone(),
            subagents: None,
            tool_search: None,
            workers: Default::default(),
            worker_registry: Default::default(),
            child: true,
            pricing: self.pricing.clone(),
            mailbox: self.mailbox.clone(),
            families: Default::default(),
            session_close: self.session_close.clone(),
            registrations: Vec::new(),
        })
    }
}
