//! [`AgentBuilder`]: binds an [`AgentSpec`] to what an agent runs with.

use std::sync::{Arc, OnceLock};

use super::spec::{self, AgentSpec};
use super::{Agent, AgentInner, BuildError, Pricing, SessionClose};
use crate::coordination::{Mailbox, SendMessageTool};
use crate::hooks::Hooks;
use crate::ids::SessionId;
use crate::json::JsonObject;
use crate::message::SystemBlock;
use crate::permissions::PermissionEngine;
use crate::provider::Provider;
use crate::registration::Registration;
use crate::registry::ToolRegistry;
use crate::skills::{self, Skill, SkillTool};
use crate::store::{RunStore, SessionStore};
use crate::subagents::{ContinueTool, Roster, StopTool, SubagentDefinition, SubagentTool, Workers};
use crate::usage::Usage;

/// Binds an [`AgentSpec`] to the values an agent runs with.
///
/// The provider, the permission engine, both stores and a namespace are
/// required; tools, hooks, skills, subagents, a mailbox, pricing and
/// session-close callbacks default to none. Nothing here does I/O.
pub struct AgentBuilder {
    spec: AgentSpec,
    provider: Option<Arc<dyn Provider>>,
    tools: Arc<ToolRegistry>,
    permissions: Option<PermissionEngine>,
    hooks: Arc<Hooks>,
    runs: Option<Arc<dyn RunStore>>,
    sessions: Option<Arc<dyn SessionStore>>,
    namespace: Option<String>,
    session_close: Vec<SessionClose>,
    skills: Option<Vec<Skill>>,
    subagents: Option<Vec<SubagentDefinition>>,
    pricing: Option<Pricing>,
    mailbox: Option<Arc<dyn Mailbox>>,
    workers: Option<Workers>,
}

impl AgentBuilder {
    /// Start binding `spec`.
    pub fn new(spec: AgentSpec) -> Self {
        Self {
            spec,
            provider: None,
            tools: Arc::new(ToolRegistry::new()),
            permissions: None,
            hooks: Arc::new(Hooks::default()),
            runs: None,
            sessions: None,
            namespace: None,
            session_close: Vec::new(),
            skills: None,
            subagents: None,
            pricing: None,
            mailbox: None,
            workers: None,
        }
    }

    /// The model provider.
    pub fn provider(mut self, provider: Arc<dyn Provider>) -> Self {
        self.provider = Some(provider);
        self
    }

    /// The tool registry; each turn takes one snapshot of it.
    pub fn tools(mut self, tools: Arc<ToolRegistry>) -> Self {
        self.tools = tools;
        self
    }

    /// The permission engine that decides every tool call.
    pub fn permissions(mut self, permissions: PermissionEngine) -> Self {
        self.permissions = Some(permissions);
        self
    }

    /// The hook chains.
    pub fn hooks(mut self, hooks: Arc<Hooks>) -> Self {
        self.hooks = hooks;
        self
    }

    /// The durable run store.
    pub fn run_store(mut self, runs: Arc<dyn RunStore>) -> Self {
        self.runs = Some(runs);
        self
    }

    /// The durable session store.
    pub fn session_store(mut self, sessions: Arc<dyn SessionStore>) -> Self {
        self.sessions = Some(sessions);
        self
    }

    /// The store key space for this agent's runs and sessions.
    pub fn namespace(mut self, namespace: impl Into<String>) -> Self {
        self.namespace = Some(namespace.into());
        self
    }

    /// Run `callback` on [`Agent::close_session`], after the callbacks
    /// added before it. A host frees per-session state there, such as the
    /// read evidence of `linch_tools::ReadTracker::forget`.
    pub fn on_session_close(
        mut self,
        callback: impl Fn(&SessionId) + Send + Sync + 'static,
    ) -> Self {
        self.session_close.push(Arc::new(callback));
        self
    }

    /// Price every paid model call with `pricing`, which gets the model
    /// that served the call (the requested one when the provider named
    /// none) and the call's usage, and gives its cost in USD, or `None`
    /// when it has no price for that model. A non-finite cost counts as
    /// `None`. The cost goes on the call's receipt and `usage` event, and
    /// the run's priced calls add up to the result's `total_cost_usd`;
    /// without pricing every cost is `None`. Pricing is not part of the
    /// run contract. A host wires `linch_providers::cost` here.
    pub fn pricing(
        mut self,
        pricing: impl Fn(&str, &Usage) -> Option<f64> + Send + Sync + 'static,
    ) -> Self {
        self.pricing = Some(Arc::new(pricing));
        self
    }

    /// Turn skills on with `skills` (see [`crate::skills`]).
    ///
    /// The built-in `verify` skill joins them unless one has its name
    /// (case-insensitively). [`Self::build`] registers the `Skill` tool in
    /// the tool registry, for as long as the agent lives, and puts the
    /// skill listing in the system prompt. Every skill is part of the run
    /// contract.
    pub fn skills(mut self, skills: Vec<Skill>) -> Self {
        self.skills = Some(skills);
        self
    }

    /// Turn foreground subagents on with `definitions` (see
    /// [`crate::subagents`]).
    ///
    /// The built-in `verification` agent joins them unless one has its name
    /// (case-insensitively). [`Self::build`] registers the `Subagent` tool
    /// in the tool registry, for as long as the agent lives. Every
    /// definition is part of the run contract.
    pub fn subagents(mut self, definitions: Vec<SubagentDefinition>) -> Self {
        self.subagents = Some(definitions);
        self
    }

    /// Keep the `Subagent` tool's workers as `workers` says (see
    /// [`crate::subagents`]); it needs [`Self::subagents`]. The setting is
    /// part of the run contract.
    pub fn workers(mut self, workers: Workers) -> Self {
        self.workers = Some(workers);
        self
    }

    /// Give the agent a mailbox for peer messages (see
    /// [`crate::coordination`]).
    ///
    /// [`Self::build`] registers the `send_message` tool in the tool
    /// registry, for as long as the agent lives, and every run (a
    /// subagent's too) drains its session's inbox at its start and at the
    /// end of each tool batch. Several agents may share one mailbox.
    pub fn mailbox(mut self, mailbox: Arc<dyn Mailbox>) -> Self {
        self.mailbox = Some(mailbox);
        self
    }

    /// Build the agent and compute its contract digest from the spec, the
    /// tools registered now, the hook policies, the skills and the
    /// subagents (BLUEPRINT 13.1.2).
    ///
    /// Raises:
    ///     BuildError: If a required binding is missing, the model or the
    ///         namespace is blank, the compaction settings cannot run, a
    ///         built-in tool's name is taken, or the tool schemas cannot be
    ///         digested.
    pub fn build(self) -> Result<Agent, BuildError> {
        if self.spec.model.trim().is_empty() {
            return Err(BuildError::Invalid("the model must not be blank".into()));
        }
        if let Some(problem) = self.spec.compaction.as_ref().and_then(|c| c.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        let namespace = self.namespace.ok_or(BuildError::Missing("namespace"))?;
        if namespace.trim().is_empty() {
            return Err(BuildError::Invalid(
                "the namespace must not be blank".into(),
            ));
        }
        let provider = self.provider.ok_or(BuildError::Missing("provider"))?;
        let permissions = (self.permissions).ok_or(BuildError::Missing("permission engine"))?;
        let runs = self.runs.ok_or(BuildError::Missing("run store"))?;
        let sessions = self.sessions.ok_or(BuildError::Missing("session store"))?;
        let mut features = JsonObject::new();
        let mut built_ins = BuiltIns::default();
        let mut listing = None;
        let mut overlays = skills::Overlays::new();
        let skills_on = self.skills.is_some();
        if let Some(skills) = self.skills {
            let skills = skills::with_builtins(skills);
            overlays = skills::overlays(&skills);
            let value =
                serde_json::to_value(&skills).map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("skills".into(), value);
            listing = listing_block(&skills);
            built_ins.register(&self.tools, Arc::new(SkillTool::new(skills)))?;
        }
        if let Some(mailbox) = &self.mailbox {
            let tool = SendMessageTool::new(Arc::clone(mailbox));
            built_ins.register(&self.tools, Arc::new(tool))?;
        }
        let handle = Arc::new(OnceLock::new());
        let mut subagents = None;
        if self.workers.is_some() && self.subagents.is_none() {
            return Err(BuildError::Invalid("workers need subagents".into()));
        }
        if let Some(workers) = self.workers {
            let value =
                serde_json::to_value(workers).map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("workers".into(), value);
        }
        let workers = self.workers.unwrap_or_default();
        if let Some(definitions) = self.subagents {
            let roster = Arc::new(Roster::new(definitions));
            let value =
                serde_json::to_value(&*roster).map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("subagents".into(), value);
            let tool = SubagentTool::new(Arc::clone(&roster), Arc::clone(&handle), workers);
            built_ins.register(&self.tools, Arc::new(tool))?;
            if workers.background || workers.retain {
                let tool = ContinueTool::new(Arc::clone(&handle));
                built_ins.register(&self.tools, Arc::new(tool))?;
            }
            if workers.background {
                let tool = StopTool::new(Arc::clone(&handle));
                built_ins.register(&self.tools, Arc::new(tool))?;
            }
            subagents = Some(roster);
        }
        let tools = self.tools.snapshot().contract_tools();
        let hooks = self.hooks.policy_digest();
        let contract = spec::contract_digest(&self.spec, &tools, hooks, &features)?;
        let mut system = spec::prompt_block(&self.spec);
        system.extend(listing.clone());
        let inner = Arc::new(AgentInner {
            provider,
            permissions,
            runs,
            sessions,
            spec: self.spec,
            tools: self.tools,
            filter: None,
            hooks: self.hooks,
            namespace,
            contract,
            system,
            features,
            skills: skills_on,
            skill_listing: listing,
            overlays: Arc::new(overlays),
            subagents,
            workers,
            worker_registry: Default::default(),
            child: false,
            pricing: self.pricing,
            mailbox: self.mailbox,
            families: Default::default(),
            session_close: self.session_close,
            registrations: built_ins.take(),
        });
        // Set once, here: nothing else holds the handle yet.
        let _ = handle.set(Arc::downgrade(&inner));
        Ok(Agent { inner })
    }
}

/// The skill listing as a cacheable system block; `None` when no skill is
/// visible to the model.
fn listing_block(skills: &[Skill]) -> Option<SystemBlock> {
    let listing = skills::listing(skills);
    (!listing.is_empty()).then(|| SystemBlock {
        text: format!(
            "The following skills are available for use with the Skill tool:\n\n{listing}"
        ),
        cacheable: true,
    })
}

/// The built-in tools registered so far; disposed again if the build
/// fails before the agent owns them.
#[derive(Default)]
struct BuiltIns(Vec<Registration>);

impl BuiltIns {
    fn register(
        &mut self,
        tools: &ToolRegistry,
        tool: Arc<dyn crate::tool::Tool>,
    ) -> Result<(), BuildError> {
        let registration = tools.register(tool).map_err(|error| {
            BuildError::Invalid(format!("the built-in tool cannot be registered: {error}"))
        })?;
        self.0.push(registration);
        Ok(())
    }

    fn take(mut self) -> Vec<Registration> {
        std::mem::take(&mut self.0)
    }
}

impl Drop for BuiltIns {
    fn drop(&mut self) {
        for registration in &self.0 {
            registration.dispose();
        }
    }
}
