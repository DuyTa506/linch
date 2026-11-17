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

use crate::ask_user::AskUserAnswer;
use crate::canonical::Digest;
use crate::coordination::Mailbox;
use crate::events::kinds::ScheduleFireEvent;
use crate::events::{Envelope, Persistence};
use crate::gates::Rules;
use crate::hooks::{ApprovalResponse, Hooks};
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::message::{Message, SystemBlock};
use crate::permissions::PermissionEngine;
use crate::policy::Principal;
use crate::provider::{
    CacheControl, OutputSchema, Provider, ProviderGate, ProviderRequest, Thinking, ToolChoice,
    ToolDefinition,
};
use crate::redaction::Redactor;
use crate::registration::Registration;
use crate::registry::{ToolRegistry, ToolSnapshot};
use crate::run::RunStatus;
use crate::runner::{self, Families, Releases, RunStream};
use crate::skills::SkillSet;
use crate::store::{RunRecord, RunStore, SessionStore};
use crate::structured::{FinalTool, OutputContract};
use crate::subagents::{Registry, Roster, Workers};
use crate::tool_cache::ToolCacheHandle;
use crate::tool_search::ToolSearch;
use crate::usage::Usage;
use crate::verification::Verifiers;

mod builder;
mod child;
mod error;
mod fork;
mod reload;
mod spec;
mod steer;
mod versions;

pub use builder::AgentBuilder;
pub use error::{BuildError, RunError};
pub use fork::ForkError;
pub use spec::AgentSpec;
pub use steer::{MAX_PENDING_STEERS, MAX_STEER_BYTES};

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
    /// The host's trace id for a new run, stored with it in its `init`
    /// event and echoed on every `init` and `result` of the run and on
    /// each [`crate::ToolContext::trace_id`]; its subagents' runs inherit
    /// it. `None` sets none. Resume and approve ignore this field and keep
    /// the run's own.
    pub trace_id: Option<String>,
    /// The output schema of this run alone, in place of the agent's
    /// `spec.output_schema` (Python's `RunOptions.output_schema`); the
    /// agent's `structured_output_retries` still bound it. `None` keeps
    /// the agent's. It is checked before anything is written and stored in
    /// the run's `init` event, so a resume or approve uses it again; they
    /// ignore this field. It is not part of the contract, which stays the
    /// agent's.
    pub output_schema: Option<OutputSchema>,
    /// The tool choice of this run alone, in place of the agent's
    /// `spec.tool_choice` (Python's `RunOptions.tool_choice`). `None` keeps
    /// the agent's. It is checked before anything is written (a named tool
    /// the run does not have, or a choice the provider refuses, fails with
    /// [`RunError::InvalidToolChoice`]) and stored in the run's `init`
    /// event, so a resume or approve uses it again; they ignore this
    /// field. It is not part of the contract.
    pub tool_choice: Option<ToolChoice>,
    /// The schedule fire that started this run, for a worker's run of a
    /// scheduled job; `None` for any other. A new run stages it as a
    /// `schedule_fire` event right after its `init`, in the same write, so
    /// the run's events name the schedule that made it. A run without it
    /// writes exactly what it wrote before. Resume and approve ignore this
    /// field.
    pub schedule: Option<ScheduleFireEvent>,
}

impl RunOptions {
    /// Options for `session_id`, with no run id, no principal, owner
    /// `linch`, a fresh cancel token, no trace id and the agent's output
    /// schema and tool choice.
    pub fn new(session_id: impl Into<SessionId>) -> Self {
        Self {
            session_id: session_id.into(),
            run_id: None,
            principal: None,
            owner: "linch".into(),
            cancel: CancellationToken::new(),
            trace_id: None,
            output_schema: None,
            tool_choice: None,
            schedule: None,
        }
    }
}

/// Why `choice` cannot be a run's tool choice on `provider` with
/// `thinking`, `known` saying whether a tool name is one the run offers and
/// `toolless` that it offers none; `None` when it can.
pub(crate) fn tool_choice_problem(
    provider: &dyn Provider,
    choice: &ToolChoice,
    thinking: Option<&Thinking>,
    known: impl Fn(&str) -> bool,
    toolless: bool,
) -> Option<String> {
    if toolless && matches!(choice, ToolChoice::Required {}) {
        return Some("tool_choice required needs a tool, and the run offers none".into());
    }
    if let ToolChoice::Tool { name } = choice
        && !known(name)
    {
        return Some(format!(
            "tool_choice names {name:?}, which is not a tool the run offers"
        ));
    }
    let refusal = provider.tool_choice_refusal(choice, thinking)?;
    Some(format!(
        "{} refuses this tool_choice: {refusal}",
        provider.id()
    ))
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
    /// The spec's `output_schema`, compiled; `None` without one.
    pub(crate) output: Option<Arc<OutputContract>>,
    /// The final tool of a run that has no output schema of its own (see
    /// [`AgentSpec::final_tool_name`]); `None` without one.
    pub(crate) final_tool: Option<FinalTool>,
    /// The spec's `redaction`, compiled; `None` without one.
    pub(crate) redactor: Option<Arc<Redactor>>,
    /// The spec's `verification`, compiled; `None` without one.
    pub(crate) verifiers: Option<Arc<Verifiers>>,
    /// The spec's `policy`, compiled; `None` without one.
    pub(crate) gates: Option<Arc<Rules>>,
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
    /// The cap on concurrent provider calls this agent's runs share, its
    /// subagents' included; `None` for no cap. Not part of the contract.
    pub(crate) gate: Option<ProviderGate>,
    /// The cache of tool results (see [`crate::tool_cache`]); `None`
    /// without `spec.tool_cache`, and for a subagent's agent.
    pub(crate) tool_cache: Option<ToolCacheHandle>,
    /// The inbox each run of a session drains; `None` without one.
    pub(crate) mailbox: Option<Arc<dyn Mailbox>>,
    /// The inbox of each run this process drives, for [`Agent::steer`].
    pub(crate) steering: crate::runner::Steering,
    /// The subagent child runs of each run that is live or suspended in
    /// this process (see [`runner::Family`]).
    pub(crate) families: Families,
    /// The lease releases of runs dropped mid-run, still in flight.
    pub(crate) releases: Releases,
    session_close: Vec<SessionClose>,
    /// Run when a run of a session records a compaction (see
    /// [`AgentBuilder::on_compact`]).
    compact: Vec<SessionClose>,
    /// The built-in tools this agent registered, removed when it goes.
    registrations: Vec<Registration>,
    /// The reloads of this agent not disposed yet, undone when it goes,
    /// before its built-in tools.
    reloads: Mutex<Vec<Arc<Registration>>>,
}

impl AgentInner {
    /// A primary request for `messages` with `system` blocks and `tools`.
    /// It asks for prompt caching, as Python's `cache_prompt=True` does: an
    /// adapter that takes breakpoints (Anthropic) marks the last cacheable
    /// system block and the last user block; the others ignore it.
    pub(crate) fn request(
        &self,
        system: Vec<SystemBlock>,
        tools: Vec<ToolDefinition>,
        messages: Vec<Message>,
    ) -> ProviderRequest {
        let mut request = self.spec.request(tools, messages);
        request.system = system;
        request.cache_control = Some(CacheControl::default());
        request
    }

    /// The redactor when it scrubs assistant responses.
    pub(crate) fn response_redactor(&self) -> Option<&Redactor> {
        (self.redactor.as_deref()).filter(|redactor| redactor.response())
    }

    /// The redactor when it scrubs tool results.
    pub(crate) fn result_redactor(&self) -> Option<&Arc<Redactor>> {
        (self.redactor.as_ref()).filter(|redactor| redactor.tool_results())
    }

    /// The redactor when it scrubs a new run's prompt.
    pub(crate) fn prompt_redactor(&self) -> Option<&Redactor> {
        (self.redactor.as_deref()).filter(|redactor| redactor.prompt())
    }

    /// The context window of the agent's model, as its provider reports it.
    pub(crate) fn context_window(&self) -> u64 {
        self.provider.context_window(&self.spec.model)
    }

    /// Run every session-close callback for `session`.
    pub(crate) fn close(&self, session: &SessionId) {
        for callback in &self.session_close {
            callback(session);
        }
    }

    /// Run every compaction callback for `session`.
    pub(crate) fn compacted(&self, session: &SessionId) {
        for callback in &self.compact {
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

/// The first model `spec` can be served by (its own, then each fallback)
/// that `pricing` has no finite price for.
pub(crate) fn first_unpriced<'a>(
    pricing: Option<&Pricing>,
    spec: &'a AgentSpec,
) -> Option<&'a str> {
    let priced = |model: &str| {
        let price = pricing.and_then(|price| price(model, &Usage::default()));
        price.is_some_and(f64::is_finite)
    };
    let models = std::iter::once(&spec.model).chain(&spec.fallback_models);
    models.map(String::as_str).find(|model| !priced(model))
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
    ///
    /// On a run store whose claims expire (Postgres), a run another process
    /// drives is not taken from it: the stream ends with
    /// `RunError::Store(StoreError::RunLeased { .. })` and nothing is
    /// written. Try again once that holder stops, at the latest when its
    /// lease runs out. A run this agent's own store holds is taken over at
    /// once, as on the memory stores.
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
    /// suspended run can still both pass this check. Through one store the
    /// later claim fences the earlier approve, then gets `NotSuspended`,
    /// which leaves the run `running` for a resume; from another process,
    /// on a store whose claims expire, the later one gets `RunLeased`.
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
        let reply = runner::Reply::Approvals(decisions);
        runner::resume(Arc::clone(&self.inner), run_id, Some(reply), options)
    }

    /// Answer the pending `AskUser` calls of a run suspended for input,
    /// and continue it.
    ///
    /// Only a run awaiting input takes an answer, and only when every
    /// answer fits its request's questions (see
    /// [`AskUserAnswer::check`]): otherwise the stream ends with
    /// `RunError::NotSuspended` or `RunError::InvalidAnswer` before
    /// anything is claimed or written, and the run stays suspended. The
    /// answers are committed as the calls' results before the run goes
    /// on; from then on a retried answer gets `NotSuspended` and the host
    /// continues the run with [`Agent::resume`]. Two answers that race can
    /// both pass the check, as two approves can.
    ///
    /// Args:
    ///     run_id: A run whose checkpoint is awaiting input.
    ///     answers: The user's answer per pending `request_id` (the
    ///         `AskUser` call's `tool_use_id`), at least one. A pending
    ///         request left out is asked again.
    ///     options: Principal, owner and cancel token for the continuation.
    pub fn answer(
        &self,
        run_id: RunId,
        answers: HashMap<String, AskUserAnswer>,
        options: RunOptions,
    ) -> RunStream {
        let reply = runner::Reply::Answers(answers);
        runner::resume(Arc::clone(&self.inner), run_id, Some(reply), options)
    }

    /// Cancel `run_id` through the run store: end a run that waits for an
    /// approval or an answer, here or in another process, with a `Result`
    /// of subtype `aborted`.
    ///
    /// A run this host drives is cancelled with the token it was given
    /// ([`RunOptions::cancel`]); this call is for a run no stream drives.
    /// It claims the run like an approve, which fences out any earlier
    /// holder, and commits an `aborted` result for each call of the last
    /// response that has none (the interrupted placeholder for one that
    /// started), with the cancel's intent, before anything else. Then the
    /// results go into the session, so a later run there sees every call
    /// answered, and the run ends with status `aborted`. A crash in
    /// between leaves the intent durable: [`Agent::resume`] or another
    /// cancel finishes the abort, and an approve or an answer gets
    /// `NotSuspended`. Background subagents the run kept here are
    /// cancelled and write their aborts. The contract is not checked: a
    /// run started under another contract can still be cancelled.
    ///
    /// A cancel and an approve (or an answer) that race both pass their
    /// checks; the later claim fences out the earlier one, so exactly one
    /// ends the run's suspension. When the approve claims last, the cancel
    /// ends with `LeaseLost` (or, if the approve already ended the run,
    /// returns `false`); when the cancel claims last, the approve's next
    /// write fails with `LeaseLost`, or its check finds the run aborted
    /// (`Terminal`). On a store whose claims expire, a cancel of a run
    /// another process drives is refused with `RunLeased`.
    ///
    /// Args:
    ///     run_id: The run to cancel.
    ///     options: The owner name for the claim; the principal reaches the
    ///         hooks of the abort. A cancel token already cancelled makes
    ///         the call do nothing.
    ///
    /// Returns:
    ///     `true` when this call ended the run; `false` when there was
    ///     nothing to cancel: the run is already terminal, or it is not
    ///     suspended (a run in flight is cancelled by its driver's token).
    ///
    /// Raises:
    ///     RunError: `Store` if the run does not exist, a store call fails,
    ///         or another process holds its lease (`RunLeased`); `Journal`
    ///         if a write fails, for example because a racing approve
    ///         claimed the run since.
    pub async fn cancel(&self, run_id: RunId, options: RunOptions) -> Result<bool, RunError> {
        let reply = Some(runner::Reply::Cancel);
        let mut stream = runner::resume(Arc::clone(&self.inner), run_id, reply, options);
        let mut ended = false;
        while let Some(item) = futures::StreamExt::next(&mut stream).await {
            match item {
                Ok(envelope) => ended |= matches!(envelope.event, crate::Event::Result(_)),
                Err(RunError::Terminal { .. } | RunError::NotSuspended { .. }) => return Ok(false),
                Err(error) => return Err(error),
            }
        }
        Ok(ended)
    }

    /// Mark `run_id` failed for good, when it can never continue (its
    /// durable state does not reconcile, or no agent has its contract).
    ///
    /// It seizes the run ([`RunStore::seize`]), which fences out any live
    /// owner, in this process or another: that owner's next write fails
    /// with a lost lease, and on a store whose claims expire its next
    /// heartbeat stops it. Then one write commits an
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

    /// Whether `run_id` is suspended in this process with subagent children
    /// still running: a future that resolves once none of them runs, else
    /// `None`.
    ///
    /// A run that suspends for approval keeps its background workers
    /// running after its stream ends. A host that counts live runs (one
    /// place per run) holds the run's place until this resolves, or until
    /// an approve or resume of the run takes the children over.
    ///
    /// Args:
    ///     run_id: The run, as its stream named it.
    ///
    /// Returns:
    ///     The wait for its children, when it is suspended here with any
    ///     running; `None` for any other run.
    pub fn parked_children(
        &self,
        run_id: &RunId,
    ) -> Option<impl Future<Output = ()> + Send + use<>> {
        self.inner.family(run_id)?.parked_children()
    }

    /// Wait until the lease releases of this agent's runs that were dropped
    /// or stopped by an error have landed (or failed), so another process
    /// can resume them at once. Call it before the process exits after
    /// dropping run streams; a release cut off by the exit leaves the run
    /// to wait out its lease. Nothing to wait for on the memory stores.
    pub async fn released(&self) {
        self.inner.releases.wait().await;
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

    /// The run's stored record: ids, status and checkpoint.
    ///
    /// Raises:
    ///     RunError: `Store` (`NotFound` for an unknown run) if the read
    ///         fails.
    pub async fn run_record(&self, run_id: &RunId) -> Result<RunRecord, RunError> {
        Ok(self.inner.runs.load(&self.inner.namespace, run_id).await?)
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
