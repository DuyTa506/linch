//! [`AgentBuilder`]: binds an [`AgentSpec`] to what an agent runs with.

use std::sync::{Arc, OnceLock};

use super::spec::{self, AgentSpec};
use super::{Agent, AgentInner, BuildError, Pricing, SessionClose, Versions, tool_choice_problem};
use crate::coordination::{Mailbox, SendMessageTool};
use crate::gates::Rules;
use crate::hooks::Hooks;
use crate::ids::SessionId;
use crate::json::JsonObject;
use crate::message::SystemBlock;
use crate::permissions::PermissionEngine;
use crate::provider::{Provider, ProviderGate};
use crate::redaction::Redactor;
use crate::registration::Registration;
use crate::registry::ToolRegistry;
use crate::skills::{Skill, SkillSet, SkillTool};
use crate::store::{RunStore, SessionStore};
use crate::structured::{FINAL_TOOL_FEATURE, FinalTool, OutputContract};
use crate::subagents::{ContinueTool, Roster, StopTool, SubagentDefinition, SubagentTool, Workers};
use crate::tool_cache::{InMemoryToolCache, ToolCacheHandle, ToolCacheStore};
use crate::tool_search::{SearchTool, ToolSearch};
use crate::usage::Usage;
use crate::verification::Verifiers;

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
    compact: Vec<SessionClose>,
    skills: Option<Vec<Skill>>,
    subagents: Option<Vec<SubagentDefinition>>,
    pricing: Option<Pricing>,
    gate: Option<ProviderGate>,
    mailbox: Option<Arc<dyn Mailbox>>,
    workers: Option<Workers>,
    tool_search: Option<ToolSearch>,
    tool_cache: Option<Arc<dyn ToolCacheStore>>,
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
            compact: Vec::new(),
            skills: None,
            subagents: None,
            pricing: None,
            gate: None,
            mailbox: None,
            workers: None,
            tool_search: None,
            tool_cache: None,
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

    /// Run `callback` each time a run of `session` records a compaction
    /// (a summary or a micro-compaction), after the callbacks added before
    /// it. The model no longer sees what was folded or cleared, so a host
    /// drops what depends on it there, such as the read evidence of
    /// `linch_tools::ReadTracker::forget`: a file must be read again before
    /// it is changed. A callback is not a hook: it is not part of the run
    /// contract and leaves no record.
    pub fn on_compact(mut self, callback: impl Fn(&SessionId) + Send + Sync + 'static) -> Self {
        self.compact.push(Arc::new(callback));
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

    /// Add `blocks` after the spec's own [`AgentSpec::system_blocks`]: host
    /// system blocks bound in code (a tool's instructions, say), sent
    /// before the system prompt with their `cacheable` flags. They become
    /// part of [`Agent::spec`] and so of the run contract.
    pub fn system_blocks(mut self, blocks: impl IntoIterator<Item = SystemBlock>) -> Self {
        self.spec.system_blocks.extend(blocks);
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

    /// Defer part of the catalog past `search`'s sizes, for the model to
    /// find with the `tool_search` tool (see [`crate::tool_search`]).
    /// [`Self::build`] registers that tool; the setting is part of the
    /// contract.
    pub fn tool_search(mut self, search: ToolSearch) -> Self {
        self.tool_search = Some(search);
        self
    }

    /// Cap the provider calls of every run of this agent, and of its
    /// subagents', at what `gate` allows (see [`ProviderGate`]). A run
    /// whose call must wait has dispatched nothing yet: it stages no
    /// delivery until it holds a place, and cancelling it while it waits
    /// aborts it with nothing written for the call. A gate shared with
    /// other agents caps them all together. It is not part of the
    /// contract, so changing the cap never strands a suspended run.
    pub fn provider_gate(mut self, gate: ProviderGate) -> Self {
        self.gate = Some(gate);
        self
    }

    /// Where the agent keeps cached tool results (see
    /// [`crate::tool_cache`]). Only a spec with `tool_cache` uses it, and
    /// without one it gets an in-memory store of its own. Agents of
    /// different namespaces may share a store: the namespace is part of
    /// every key.
    pub fn tool_cache_store(mut self, store: Arc<dyn ToolCacheStore>) -> Self {
        self.tool_cache = Some(store);
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

    /// Whether the host's pricing has a finite price for `model`.
    fn prices(&self, model: &str) -> bool {
        let price = (self.pricing.as_ref()).and_then(|price| price(model, &Usage::default()));
        price.is_some_and(f64::is_finite)
    }

    /// Build the agent and compute its contract digest from the spec, the
    /// tools registered now, the hook policies, the skills and the
    /// subagents.
    ///
    /// Raises:
    ///     BuildError: If a required binding is missing, the model or the
    ///         namespace is blank, the output schema does not compile (or
    ///         its root is not an object), a redaction rule or verifier
    ///         cannot run, the
    ///         compaction, budget (a cost cap on a model with no price
    ///         included) or
    ///         provider retry settings cannot run, the tool call timeout
    ///         is under 100 ms, the loop guard allows no
    ///         turn without a provider call, the tool retry or tool cache policy cannot run, a fallback model is blank
    ///         or repeats a model, a built-in tool's name is taken, the
    ///         final tool is not registered (or, made from the output
    ///         schema, its name is taken or unusable), the tool choice names
    ///         a tool the agent does not offer or its provider refuses it, or
    ///         the tool schemas cannot be digested, a system section is
    ///         blank, repeated or not cacheable before a cacheable block,
    ///         or the context budget cannot apply.
    pub fn build(self) -> Result<Agent, BuildError> {
        if self.spec.model.trim().is_empty() {
            return Err(BuildError::Invalid("the model must not be blank".into()));
        }
        if (self.spec.system_blocks.iter()).any(|block| block.text.trim().is_empty()) {
            return Err(BuildError::Invalid(
                "a system block must not be blank".into(),
            ));
        }
        if let Some(problem) = self.spec.system.problem(&self.spec.system_blocks) {
            return Err(BuildError::Invalid(problem));
        }
        if let Some(problem) = (self.spec.context_budget.as_ref()).and_then(|b| b.problem()) {
            return Err(BuildError::Invalid(problem));
        }
        if let Some(problem) = self.spec.compaction.as_ref().and_then(|c| c.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        let truncation = self.spec.truncation_recovery.as_ref();
        if let Some(problem) = truncation.and_then(|t| t.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        if let Some(problem) = self.spec.budget.as_ref().and_then(|b| b.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        let costed = (self.spec.budget.as_ref()).is_some_and(|b| b.max_cost_usd.is_some());
        if costed && !self.prices(&self.spec.model) {
            return Err(BuildError::Invalid(format!(
                "budget.max_cost_usd needs a price for the model {:?}, which has none: set \
                 `pricing` for it or cap budget.max_tokens instead",
                self.spec.model
            )));
        }
        if let Some(problem) = self.spec.call_timeout_problem() {
            return Err(BuildError::Invalid(problem.into()));
        }
        if let Some(problem) = self.spec.loop_guard.problem() {
            return Err(BuildError::Invalid(problem.into()));
        }
        if let Some(problem) = self.spec.tool_retry.as_ref().and_then(|r| r.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        if let Some(problem) = self.spec.tool_cache.as_ref().and_then(|c| c.problem()) {
            return Err(BuildError::Invalid(problem.into()));
        }
        if let Some(problem) = self.spec.provider_retry.problem() {
            return Err(BuildError::Invalid(problem.into()));
        }
        let (model, fallbacks) = (&self.spec.model, &self.spec.fallback_models);
        if let Some(problem) = crate::reliability::fallback_problem(model, fallbacks) {
            return Err(BuildError::Invalid(problem.into()));
        }
        // Retries without a schema bound the runs that bring their own.
        let output = match self.spec.output_schema.clone() {
            Some(schema) => Some(Arc::new(
                OutputContract::compile(schema).map_err(BuildError::Invalid)?,
            )),
            None => None,
        };
        let redactor = match &self.spec.redaction {
            Some(spec) => Some(Arc::new(Redactor::new(spec).map_err(BuildError::Invalid)?)),
            None => None,
        };
        let verifiers = match &self.spec.verification {
            Some(spec) => Some(Arc::new(Verifiers::new(spec).map_err(BuildError::Invalid)?)),
            None => None,
        };
        let namespace = self.namespace.ok_or(BuildError::Missing("namespace"))?;
        if namespace.trim().is_empty() {
            return Err(BuildError::Invalid(
                "the namespace must not be blank".into(),
            ));
        }
        let provider = self.provider.ok_or(BuildError::Missing("provider"))?;
        if let Some(problem) = (self.spec.temperature)
            .and_then(|temperature| spec::temperature_problem(temperature, &*provider))
        {
            return Err(BuildError::Invalid(problem));
        }
        let permissions = (self.permissions).ok_or(BuildError::Missing("permission engine"))?;
        let runs = self.runs.ok_or(BuildError::Missing("run store"))?;
        let sessions = self.sessions.ok_or(BuildError::Missing("session store"))?;
        let mut features = JsonObject::new();
        let mut built_ins = BuiltIns::default();
        let mut skill_versions = None;
        if let Some(skills) = self.skills {
            let window = provider.context_window(&self.spec.model);
            let set = SkillSet::new(skills, window)?;
            let value = serde_json::to_value(&set.skills)
                .map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("skills".into(), value);
            let versions = Versions::new(Arc::new(set));
            let tool = SkillTool::new(Arc::clone(&versions));
            built_ins.register(&self.tools, Arc::new(tool))?;
            skill_versions = Some(versions);
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
            subagents = Some(Versions::new(Arc::clone(&roster)));
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
        }
        if let Some(search) = self.tool_search {
            let value =
                serde_json::to_value(search).map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("tool_search".into(), value);
            let tool = SearchTool::new(Arc::clone(&handle));
            built_ins.register(&self.tools, Arc::new(tool))?;
        }
        // A policy that allows more than the default must not continue a
        // run started under a stricter one; the default adds nothing, so
        // the runs of default-policy agents keep their contracts.
        let policy = permissions.policy();
        if !policy.is_default() {
            let value =
                serde_json::to_value(policy).map_err(|e| BuildError::Invalid(e.to_string()))?;
            features.insert("permissions".into(), value);
        }
        let built_tools = self.tools.snapshot();
        let registered = |name: &str| built_tools.resolve(name).is_some();
        // A hit skips the tool's run, so one whose run keeps session state
        // (`Read`'s record of what the session saw) cannot be cached. A name
        // that is not registered yet may be later; the scheduler checks again.
        for name in self.spec.tool_cache.iter().flat_map(|cache| &cache.tools) {
            if built_tools
                .resolve(name)
                .is_some_and(|tool| !tool.tool().cache_safe())
            {
                return Err(BuildError::Invalid(format!(
                    "tool_cache.tools names {name:?}, whose run keeps session state that a \
                     cache hit would skip; leave it out"
                )));
            }
        }
        let native = provider.native_output_schema();
        let named = self.spec.final_tool_name.as_deref();
        let final_tool = FinalTool::of(named, output.as_deref(), native);
        if let Some(tool) = &final_tool {
            if let Some(problem) = tool.problem(registered) {
                return Err(BuildError::Invalid(problem));
            }
            // An agent whose provider takes its schema natively keeps the
            // contract it had before the final tool existed.
            if tool.made.is_some() {
                features.insert(FINAL_TOOL_FEATURE.into(), tool.name.clone().into());
            }
        }
        if let Some(choice) = &self.spec.tool_choice {
            let offered = |name: &str| {
                registered(name) || final_tool.as_ref().is_some_and(|t| t.name == name)
            };
            let thinking = self.spec.thinking.as_ref();
            let toolless = built_tools.definitions().is_empty() && final_tool.is_none();
            if let Some(problem) =
                tool_choice_problem(&*provider, choice, thinking, offered, toolless)
            {
                return Err(BuildError::Invalid(problem));
            }
        }
        let gates = match &self.spec.policy {
            Some(policy) => {
                let offered = |name: &str| {
                    registered(name) || final_tool.as_ref().is_some_and(|t| t.name == name)
                };
                let named = final_tool.as_ref().map(|tool| tool.name.as_str());
                if let Some(problem) = policy.problem(offered, named) {
                    return Err(BuildError::Invalid(problem));
                }
                let choice = self.spec.tool_choice.as_ref();
                if let Some(problem) = choice.and_then(|c| policy.choice_problem(c, named)) {
                    return Err(BuildError::Invalid(problem));
                }
                Some(Arc::new(
                    Rules::new(policy.clone()).map_err(BuildError::Invalid)?,
                ))
            }
            None => None,
        };
        let built_hooks = self.hooks.policy_digest();
        let tools = built_tools.contract_tools();
        let tool_cache = self.spec.tool_cache.clone().map(|config| {
            let store = (self.tool_cache).unwrap_or_else(|| Arc::new(InMemoryToolCache::new()));
            ToolCacheHandle::new(config, store, namespace.clone())
        });
        let contract = spec::contract_digest(&self.spec, &tools, built_hooks, &features)?;
        let layout = crate::system::layout(&self.spec);
        let system = layout.blocks;
        let listing_at = skill_versions.as_ref().map(|_| layout.listing_at);
        let inner = Arc::new(AgentInner {
            provider,
            permissions,
            runs,
            sessions,
            spec: self.spec,
            output,
            final_tool,
            redactor,
            verifiers,
            gates,
            tools: self.tools,
            filter: None,
            hooks: self.hooks,
            namespace,
            contract,
            built_tools,
            built_hooks,
            system,
            listing_at,
            features,
            skills: skill_versions,
            subagents,
            tool_search: self.tool_search,
            tool_cache,
            workers,
            worker_registry: Default::default(),
            child: false,
            pricing: self.pricing,
            gate: self.gate,
            mailbox: self.mailbox,
            families: Default::default(),
            steering: Default::default(),
            releases: Default::default(),
            session_close: self.session_close,
            compact: self.compact,
            registrations: built_ins.take(),
            reloads: Default::default(),
        });
        // Set once, here: nothing else holds the handle yet.
        let _ = handle.set(Arc::downgrade(&inner));
        Ok(Agent { inner })
    }
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
