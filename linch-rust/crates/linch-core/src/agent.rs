//! The agent: a serde [`AgentSpec`] bound by [`AgentBuilder`] to a provider,
//! tools, permissions, hooks and stores, and the run API on top of it.
//!
//! [`Agent::run`], [`Agent::resume`] and [`Agent::approve`] each return a
//! caller-driven [`RunStream`]: the run advances only while the stream is
//! polled, and dropping the stream drops the run, which stays resumable from
//! its last durable state. There is no name lookup and no global state;
//! every binding is a value the host passes in.

use std::collections::HashMap;
use std::sync::Arc;

use tokio_util::sync::CancellationToken;

use crate::canonical::Digest;
use crate::events::{Envelope, Persistence};
use crate::hooks::{ApprovalResponse, Hooks};
use crate::ids::{RunId, SessionId};
use crate::permissions::PermissionEngine;
use crate::policy::Principal;
use crate::provider::Provider;
use crate::registry::ToolRegistry;
use crate::runner::{self, RunStream};
use crate::store::{RunStore, SessionStore};

mod error;
mod spec;

pub use error::{BuildError, RunError};
pub use spec::AgentSpec;

/// Per-call options for [`Agent::run`], [`Agent::resume`] and
/// [`Agent::approve`].
#[derive(Debug, Clone)]
pub struct RunOptions {
    /// Session a new run belongs to. Resume and approve use the session
    /// stored with the run and ignore this field.
    pub session_id: SessionId,
    /// Id for a new run; `None` picks a fresh uuid. An id that already
    /// exists fails with [`StoreError::Conflict`] and writes nothing. Resume
    /// and approve ignore this field.
    ///
    /// [`StoreError::Conflict`]: crate::store::StoreError::Conflict
    pub run_id: Option<RunId>,
    /// Caller identity; permission rules match on its roles.
    pub principal: Option<Principal>,
    /// Lease owner name written with the claim, for diagnostics.
    pub owner: String,
    /// Aborts the run: it ends with `Result{subtype: aborted}`.
    pub cancel: CancellationToken,
}

impl RunOptions {
    /// Options for `session_id`, with no run id, no principal, owner `linch`
    /// and a fresh cancel token.
    pub fn new(session_id: impl Into<SessionId>) -> Self {
        Self {
            session_id: session_id.into(),
            run_id: None,
            principal: None,
            owner: "linch".into(),
            cancel: CancellationToken::new(),
        }
    }
}

/// Binds an [`AgentSpec`] to the values an agent runs with.
///
/// The provider, the permission engine, both stores and a namespace are
/// required; tools and hooks default to empty. Nothing here does I/O.
pub struct AgentBuilder {
    spec: AgentSpec,
    provider: Option<Arc<dyn Provider>>,
    tools: Arc<ToolRegistry>,
    permissions: Option<PermissionEngine>,
    hooks: Arc<Hooks>,
    runs: Option<Arc<dyn RunStore>>,
    sessions: Option<Arc<dyn SessionStore>>,
    namespace: Option<String>,
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

    /// Build the agent and compute its contract digest from the spec, the
    /// tools registered now and the hook policies (BLUEPRINT 13.1.2).
    ///
    /// Raises:
    ///     BuildError: If a required binding is missing, the model or the
    ///         namespace is blank, the compaction settings cannot run, or
    ///         the tool schemas cannot be digested.
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
        let definitions = self.tools.snapshot().definitions();
        let contract = spec::contract_digest(&self.spec, &definitions, self.hooks.policy_digest())?;
        let inner = AgentInner {
            provider: self.provider.ok_or(BuildError::Missing("provider"))?,
            permissions: self
                .permissions
                .ok_or(BuildError::Missing("permission engine"))?,
            runs: self.runs.ok_or(BuildError::Missing("run store"))?,
            sessions: self.sessions.ok_or(BuildError::Missing("session store"))?,
            spec: self.spec,
            tools: self.tools,
            hooks: self.hooks,
            namespace,
            contract,
        };
        Ok(Agent {
            inner: Arc::new(inner),
        })
    }
}

/// A built agent. Cheap to clone; clones share every binding.
#[derive(Clone)]
pub struct Agent {
    inner: Arc<AgentInner>,
}

/// Everything a run reads from its agent.
pub(crate) struct AgentInner {
    pub(crate) spec: AgentSpec,
    pub(crate) provider: Arc<dyn Provider>,
    pub(crate) tools: Arc<ToolRegistry>,
    pub(crate) permissions: PermissionEngine,
    pub(crate) hooks: Arc<Hooks>,
    pub(crate) runs: Arc<dyn RunStore>,
    pub(crate) sessions: Arc<dyn SessionStore>,
    pub(crate) namespace: String,
    pub(crate) contract: Digest,
}

impl Agent {
    /// The spec this agent was built from.
    pub fn spec(&self) -> &AgentSpec {
        &self.inner.spec
    }

    /// The contract digest every run of this agent stores.
    pub fn contract(&self) -> Digest {
        self.inner.contract
    }

    /// The store key space of this agent's runs and sessions.
    pub fn namespace(&self) -> &str {
        &self.inner.namespace
    }

    /// Start a new run on `prompt` in `options.session_id`, creating the
    /// session if it does not exist.
    ///
    /// Returns:
    ///     The run's stream. Its [`RunStream::run_id`] is known at once
    ///     (`options.run_id`, or a fresh uuid), so a caller that drops the
    ///     stream early can still resume the run.
    pub fn run(&self, prompt: impl Into<String>, options: RunOptions) -> RunStream {
        runner::start(Arc::clone(&self.inner), prompt.into(), options)
    }

    /// Continue `run_id` after a crash, a lost lease or a dropped stream.
    ///
    /// A run waiting for approval plans its tool calls again with the
    /// decisions recorded so far, and suspends again if one still needs an
    /// answer.
    pub fn resume(&self, run_id: RunId, options: RunOptions) -> RunStream {
        runner::resume(Arc::clone(&self.inner), run_id, None, options)
    }

    /// Answer the pending tool calls of a suspended run and continue it.
    ///
    /// Args:
    ///     run_id: A run whose checkpoint is awaiting approval.
    ///     decisions: The host's answer per pending `tool_use_id`. Answers
    ///         for calls that are not pending are ignored; a pending call
    ///         without an answer is asked again.
    ///     options: Principal, owner and cancel token for the continuation.
    pub fn approve(
        &self,
        run_id: RunId,
        decisions: HashMap<String, ApprovalResponse>,
        options: RunOptions,
    ) -> RunStream {
        runner::resume(Arc::clone(&self.inner), run_id, Some(decisions), options)
    }

    /// The run's durable envelopes with seq greater than `after_seq`,
    /// without the journal-only kinds (model-call receipts and deliveries).
    ///
    /// Raises:
    ///     RunError: If the store read fails.
    pub async fn events(&self, run_id: &RunId, after_seq: u64) -> Result<Vec<Envelope>, RunError> {
        let events = self
            .inner
            .runs
            .events(&self.inner.namespace, run_id, after_seq)
            .await?;
        Ok(events
            .into_iter()
            .filter(|envelope| envelope.event.persistence() != Persistence::JournalOnly)
            .collect())
    }
}
