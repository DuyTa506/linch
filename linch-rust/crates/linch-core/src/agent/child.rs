//! The agent a subagent's child run runs with, derived from its parent's.

use std::sync::Arc;

use super::spec::contract_digest;
use super::{Agent, AgentInner, BuildError, RunOptions};
use crate::gates::Rules;
use crate::ids::RunId;
use crate::message::SystemBlock;
use crate::registry::ToolSnapshot;
use crate::result_cap::READ_TOOL;
use crate::runner::{self, RunStream};
use crate::skills::SKILL_TOOL;
use crate::structured::FINAL_TOOL_FEATURE;
use crate::subagents::{CONTINUE_TOOL, STOP_TOOL, SUBAGENT_TOOL, SubagentDefinition};
use crate::tool_search::TOOL_SEARCH_TOOL;

/// Which of the parent's tools a child run sees: never `Subagent` (a child
/// cannot spawn), the worker tools (it has no workers) nor `tool_search`
/// (it is offered every tool), and only the named ones when the
/// definition names any, but always `ReadToolResult`, which the markers of
/// a cut result name.
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
            && (self.only.as_ref())
                .is_none_or(|only| name == READ_TOOL || only.iter().any(|n| n == name))
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
        runner::start_child(agent, prompt.into(), options, parent_run_id, None)
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
    /// bindings and spec (less its output schema), the child's tools, the spec's system blocks (the
    /// host's, then the system prompt) with the skill listing when the
    /// child has the `Skill` tool, then the definition's body, and a contract that also covers the definition.
    /// It offers no subagents, so its runs cannot spawn.
    ///
    /// The contract is digested from the tools and hook policies this
    /// agent's own contract was built from, so a tool or hook registered
    /// since does not change it; the child's runs still use the live tools,
    /// as the parent's do.
    ///
    /// Raises:
    ///     BuildError: If the definition or the tools cannot be digested.
    pub(crate) fn child(&self, definition: &SubagentDefinition) -> Result<Self, BuildError> {
        let filter = ChildTools::of(definition);
        let tools = filter.apply(&self.built_tools);
        let layout = crate::system::layout(&self.spec);
        let mut system = layout.blocks;
        let listing_at = (tools.resolve(SKILL_TOOL).is_some()).then_some(layout.listing_at);
        system.push(SystemBlock {
            text: format!("User-provided instructions:\n\n{}", definition.body),
            cacheable: true,
        });
        let mut features = self.features.clone();
        let value =
            serde_json::to_value(definition).map_err(|e| BuildError::Invalid(e.to_string()))?;
        features.insert("subagent".into(), value);
        let hooks = self.built_hooks;
        // The parent's final-answer schema, final tool, tool choice and
        // verifiers are not the child's: a subagent answers its parent in
        // text. Its redaction is:
        // the child's output reaches the same logs.
        let mut spec = self.spec.clone();
        spec.output_schema = None;
        spec.structured_output_retries = 0;
        spec.verification = None;
        spec.tool_choice = None;
        spec.final_tool_name = None;
        // Its prerequisites hold over its own calls; its gates are the
        // parent's answer to meet, with tools the child may not have.
        spec.policy = (spec.policy.as_ref())
            .map(|policy| policy.for_child())
            .filter(|policy| !policy.prerequisites.is_empty());
        let gates = match &spec.policy {
            Some(policy) => Some(Arc::new(
                Rules::new(policy.clone()).map_err(BuildError::Invalid)?,
            )),
            None => None,
        };
        features.remove(FINAL_TOOL_FEATURE);
        let contract = contract_digest(&spec, &tools.contract_tools(), hooks, &features)?;
        Ok(Self {
            spec,
            output: None,
            final_tool: None,
            redactor: self.redactor.clone(),
            verifiers: None,
            gates,
            provider: Arc::clone(&self.provider),
            tools: Arc::clone(&self.tools),
            filter: Some(filter),
            permissions: self.permissions.clone(),
            hooks: Arc::clone(&self.hooks),
            runs: Arc::clone(&self.runs),
            sessions: Arc::clone(&self.sessions),
            namespace: self.namespace.clone(),
            contract,
            built_tools: Arc::new(tools),
            built_hooks: hooks,
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
            gate: self.gate.clone(),
            tool_cache: None,
            mailbox: self.mailbox.clone(),
            families: Default::default(),
            steering: Default::default(),
            releases: self.releases.clone(),
            session_close: self.session_close.clone(),
            compact: self.compact.clone(),
            registrations: Vec::new(),
            reloads: Default::default(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The marker of a cut result names `ReadToolResult`, so a child that
    /// names its tools still has it.
    #[test]
    fn a_child_with_named_tools_keeps_the_read_back_tool() {
        let named = ChildTools {
            only: Some(vec!["Read".into()]),
        };
        assert!(named.keeps("Read") && named.keeps(READ_TOOL));
        assert!(!named.keeps("Bash") && !named.keeps(SUBAGENT_TOOL));
    }
}
