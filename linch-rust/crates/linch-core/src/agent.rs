//! The agent: a serde [`AgentSpec`] bound by [`AgentBuilder`] to a provider,
//! tools, permissions, hooks and stores, and the run API on top of it.
//!
//! [`Agent::run`], [`Agent::resume`] and [`Agent::approve`] each return a
//! caller-driven [`RunStream`]: the run advances only while the stream is
//! polled, and dropping the stream drops the run, which stays resumable from
//! its last durable state. There is no name lookup and no global state;
//! every binding is a value the host passes in.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};

use tokio_util::sync::CancellationToken;

use crate::canonical::Digest;
use crate::coordination::Mailbox;
use crate::events::{Envelope, Persistence};
use crate::hooks::{ApprovalResponse, Hooks};
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::message::{Message, SystemBlock};
use crate::permissions::PermissionEngine;
use crate::policy::Principal;
use crate::provider::{Provider, ProviderRequest, ToolDefinition};
use crate::registration::Registration;
use crate::registry::{ToolRegistry, ToolSnapshot};
use crate::run::RunStatus;
use crate::runner::{self, Families, RunStream};
use crate::skills::SkillSet;
use crate::store::{RunStore, SessionStore};
use crate::subagents::{Registry, Roster, Workers};
use crate::tool_search::ToolSearch;
use crate::usage::Usage;

mod builder;
mod child;
mod error;
mod fork;
mod reload;
mod spec;
mod versions;

pub use builder::AgentBuilder;
pub use error::{BuildError, RunError};
pub use fork::ForkError;
pub use spec::AgentSpec;

pub(crate) use child::ChildTools;
pub(crate) use versions::Versions;

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
    /// Aborts the run: it ends with `Result{subtype: aborted}`. A resume or
    /// approve given a token that is already cancelled does nothing: it
    /// claims and writes nothing, and its stream ends with no item.
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

/// The host's price of a model call (see [`AgentBuilder::pricing`]).
type Pricing = Arc<dyn Fn(&str, &Usage) -> Option<f64> + Send + Sync>;

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
    /// The tools the contract was digested from at build (a subagent's
    /// agent: its share of the parent's); a child's contract derives from
    /// them, never from the live registry.
    pub(crate) built_tools: Arc<ToolSnapshot>,
    /// The hook policy digest the contract was built with.
    pub(crate) built_hooks: Digest,
    /// The system blocks a new session's prefix takes, but for the skill
    /// listing (see [`AgentInner::system`]).
    pub(crate) system: Vec<SystemBlock>,
    /// Where the current skill listing goes in `system`; `None` when the
    /// agent lists no skills.
    pub(crate) listing_at: Option<usize>,
    /// What the contract covers beyond the spec, tools and hooks.
    pub(crate) features: JsonObject,
    /// The agent's skills, which [`Agent::reload_skills`] replaces; `None`
    /// when skills are off.
    pub(crate) skills: Option<Arc<Versions<SkillSet>>>,
    /// The subagents the `Subagent` tool runs, which
    /// [`Agent::reload_subagents`] replaces; `None` when they are off.
    pub(crate) subagents: Option<Arc<Versions<Roster>>>,
    /// When tool search defers part of the catalog; `None` when it is off.
    pub(crate) tool_search: Option<ToolSearch>,
    /// How the `Subagent` tool keeps its workers.
    pub(crate) workers: Workers,
    /// The workers of each parent session.
    pub(crate) worker_registry: Registry,
    /// Whether this is a subagent's agent: its runs cannot suspend, so a
    /// call that needs approval is denied.
    pub(crate) child: bool,
    /// Prices each paid call; `None` reports no cost.
    pub(crate) pricing: Option<Pricing>,
    /// The inbox each run of a session drains; `None` without one.
    pub(crate) mailbox: Option<Arc<dyn Mailbox>>,
    /// The subagent child runs of each run that is live or suspended in
    /// this process (see [`runner::Family`]).
    pub(crate) families: Families,
    session_close: Vec<SessionClose>,
    /// The built-in tools this agent registered, removed when it goes.
    registrations: Vec<Registration>,
    /// The reloads of this agent not disposed yet, undone when it goes,
    /// before its built-in tools.
    reloads: Mutex<Vec<Arc<Registration>>>,
}

impl AgentInner {
    /// A request for `messages` with `system` blocks and `tools`.
    pub(crate) fn request(
        &self,
        system: Vec<SystemBlock>,
        tools: Vec<ToolDefinition>,
        messages: Vec<Message>,
    ) -> ProviderRequest {
        let mut request = self.spec.request(tools, messages);
        request.system = system;
        request
    }

    /// Run every session-close callback for `session`.
    pub(crate) fn close(&self, session: &SessionId) {
        for callback in &self.session_close {
            callback(session);
        }
    }

    /// What `usage` on `model` cost in USD, by the host's pricing; `None`
    /// without pricing, for a model it has no price for, or for a price
    /// that is not finite.
    pub(crate) fn price(&self, model: &str, usage: &Usage) -> Option<f64> {
        let price = (self.pricing.as_ref()?)(model, usage)?;
        price.is_finite().then_some(price)
    }
}

impl Drop for AgentInner {
    fn drop(&mut self) {
        // The children of a suspended run stop and write their aborts.
        self.close_all_families();
        let reloads = std::mem::take(
            self.reloads
                .get_mut()
                .unwrap_or_else(PoisonError::into_inner),
        );
        for reload in reloads.iter().rev() {
            reload.dispose();
        }
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
    /// Only a run awaiting approval takes an approve; any other run gets
    /// `RunError::NotSuspended` before anything is claimed or written, so an
    /// approve never fences out the run's live owner. Once an approve has
    /// committed its answers the run is no longer suspended: retrying that
    /// approve (after a timeout, a crash, or a second click) gets
    /// `NotSuspended`, and the host continues the run with [`Agent::resume`],
    /// under the committed decisions. Two approves that race on one
    /// suspended run can still both pass this check: the later claim fences
    /// the earlier approve, then gets `NotSuspended`, which leaves the run
    /// `running` for a resume.
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
    /// reports `usage_complete: false`. Background subagents this process
    /// still runs for it (it was suspended here) are cancelled after the
    /// commit and write their aborts.
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
        let ended = runner::fail(&self.inner, run_id.clone(), reason.into(), options).await?;
        // Background children this process still drives for it stop.
        if let Some(family) = self.inner.family(&run_id) {
            family.close();
        }
        Ok(ended)
    }

    /// Tell the agent the host is done with `session_id`: run every
    /// [`AgentBuilder::on_session_close`] callback, in the order added,
    /// first for the session of each subagent worker the session started
    /// (in this process), then for the session itself. The workers are
    /// forgotten: `SubagentContinue` no longer finds them.
    ///
    /// Nothing else is written: the session logs and runs stay, and a later
    /// run on the session works as before. A run of the session that is
    /// suspended here has its background subagents cancelled first; each
    /// writes its abort, and an approve or resume of the run later hears of
    /// them.
    pub fn close_session(&self, session_id: &SessionId) {
        self.inner.close_families(session_id);
        for worker in self.inner.worker_registry.close(session_id) {
            self.inner.close(&worker);
        }
        self.inner.close(session_id);
    }

    /// The status of `run_id`, from its record alone.
    ///
    /// Raises:
    ///     RunError: `Store` (`NotFound` for an unknown run) if the read
    ///         fails.
    pub async fn status(&self, run_id: &RunId) -> Result<RunStatus, RunError> {
        let record = self.inner.runs.load(&self.inner.namespace, run_id).await?;
        Ok(record.status)
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
