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
use crate::json::JsonObject;
use crate::message::{Message, SystemBlock};
use crate::permissions::PermissionEngine;
use crate::policy::Principal;
use crate::provider::{Provider, ProviderRequest, ToolDefinition};
use crate::registration::Registration;
use crate::registry::ToolRegistry;
use crate::runner::{self, RunStream};
use crate::store::{RunStore, SessionStore};
use crate::subagents::Roster;

mod builder;
mod child;
mod error;
mod spec;

pub use builder::AgentBuilder;
pub use error::{BuildError, RunError};
pub use spec::AgentSpec;

pub(crate) use child::ChildTools;

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
    /// The tools a subagent's run sees of `tools`; `None` for all.
    pub(crate) filter: Option<ChildTools>,
    pub(crate) permissions: PermissionEngine,
    pub(crate) hooks: Arc<Hooks>,
    pub(crate) runs: Arc<dyn RunStore>,
    pub(crate) sessions: Arc<dyn SessionStore>,
    pub(crate) namespace: String,
    pub(crate) contract: Digest,
    /// The system blocks of every request: the system prompt, then the
    /// skill listing.
    pub(crate) system: Vec<SystemBlock>,
    /// What the contract covers beyond the spec, tools and hooks.
    pub(crate) features: JsonObject,
    /// Whether skills are on: the agent registered the `Skill` tool.
    pub(crate) skills: bool,
    /// The skill listing block; `None` when skills are off or none is
    /// visible to the model.
    pub(crate) skill_listing: Option<SystemBlock>,
    /// The subagents the `Subagent` tool runs; `None` when they are off.
    pub(crate) subagents: Option<Arc<Roster>>,
    /// Whether this is a subagent's agent: its runs cannot suspend, so a
    /// call that needs approval is denied.
    pub(crate) child: bool,
    session_close: Vec<SessionClose>,
    /// The built-in tools this agent registered, removed when it goes.
    registrations: Vec<Registration>,
}

impl AgentInner {
    /// A request for `messages` with this agent's system blocks and `tools`.
    pub(crate) fn request(
        &self,
        tools: Vec<ToolDefinition>,
        messages: Vec<Message>,
    ) -> ProviderRequest {
        let mut request = self.spec.request(tools, messages);
        request.system = self.system.clone();
        request
    }
}

impl Drop for AgentInner {
    fn drop(&mut self) {
        for registration in &self.registrations {
            registration.dispose();
        }
    }
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
    /// A session holds at most one run that is not terminal. On a session
    /// that has one, the stream ends with
    /// `RunError::Store(StoreError::SessionBusy { session_id, run_id })`,
    /// naming that run, and this run is never created; resume, approve or
    /// fail the named run first.
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
    /// Retrying an approve that failed after its answers were committed is
    /// safe: when the run's batch already decided every call answered, the
    /// run continues like a resume and the committed decisions stand.
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
