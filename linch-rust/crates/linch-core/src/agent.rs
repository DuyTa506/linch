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

/// A callback that [`Agent::close_session`] runs.
type SessionClose = Arc<dyn Fn(&SessionId) + Send + Sync>;

/// Binds an [`AgentSpec`] to the values an agent runs with.
///
/// The provider, the permission engine, both stores and a namespace are
/// required; tools, hooks and session-close callbacks default to none.
/// Nothing here does I/O.
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
            session_close: self.session_close,
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
    session_close: Vec<SessionClose>,
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

    /// Mark `run_id` failed for good, when it can never continue (its
    /// durable state does not reconcile, or no agent has its contract).
    ///
    /// It claims the run, which fences out any live owner: that owner's
    /// next write fails with a lost lease. Then one write commits an
    /// `Error` holding `reason`, the `Result` (subtype `error`, with the
    /// checkpoint's usage) and status `failed`. The contract is not
    /// checked. A run whose last provider or summary call may be in flight
    /// reports `usage_complete: false`.
    ///
    /// Args:
    ///     run_id: A run of this agent's namespace that is not terminal.
    ///     reason: Why the run failed; the `Error` event's message.
    ///     options: The owner name for the claim; the other fields are
    ///         ignored.
    ///
    /// Returns:
    ///     The `Error` and `Result` envelopes, now durable.
    ///
    /// Raises:
    ///     RunError: `Terminal` if the run already finished; `Store` if it
    ///         does not exist or a store call fails; `Journal` if the commit
    ///         fails, for example because another owner claimed it since.
    pub async fn fail(
        &self,
        run_id: RunId,
        reason: impl Into<String>,
        options: RunOptions,
    ) -> Result<Vec<Envelope>, RunError> {
        runner::fail(&self.inner, run_id, reason.into(), options).await
    }

    /// Tell the agent the host is done with `session_id`: run every
    /// [`AgentBuilder::on_session_close`] callback, in the order added.
    ///
    /// Nothing is written: the session log and its runs stay, and a later
    /// run on the session works as before.
    pub fn close_session(&self, session_id: &SessionId) {
        for callback in &self.inner.session_close {
            callback(session_id);
        }
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
