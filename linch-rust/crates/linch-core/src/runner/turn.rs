//! [`Runner`]: one run's state and the provider half of a turn.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio_util::sync::CancellationToken;

use super::cancel::Unpaired;
use super::end::cut_empty;
use super::final_tool::Unanswered;
use super::gate::Slot;
use super::hooks::{AfterCall, BeforeCall, Stopping, add_context, retry_message};
use super::inbox::HeldPeers;
use super::progress::Progress;
use super::retry::Recovered;
use super::structured::Checked;
use super::write::session_log;
use super::{Family, Outbox};
use crate::agent::{AgentInner, RunError};
use crate::budget::{Meter, Refusal};
use crate::catalog::Catalog;
use crate::events::Event;
use crate::events::kinds::{AssistantEvent, ResultSubtype};
use crate::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use crate::journal::{Journal, ToolRecovery};
use crate::loop_guard::Watch;
use crate::message::Message;
use crate::provider::ToolChoice;
use crate::provider::call::ModelCall;
use crate::provider::{AssistantAssembly, ProviderError};
use crate::registry::ToolSnapshot;
use crate::reliability::Standing;
use crate::run::{Ending, Phase, RunStatus, StopReason, ToolDecisionRecord};
use crate::scheduler::{RecordedApproval, RunIdentity};
use crate::session_log::{LogEntry, SessionLog};
use crate::skills;
use crate::structured::{FinalTool, OutputContract};
use crate::tool_retry::RetryClock;
use crate::usage::Usage;

/// What the run does next.
pub(super) enum Step {
    /// Append the prompt unless `appended`, then start the turn. `moved`:
    /// the session grew past the `Started` checkpoint before this run was
    /// created, so `Started` is saved again at its length first.
    Prompt {
        prompt: Message,
        appended: bool,
        moved: bool,
    },
    /// Compact the provider view if it passed the threshold, with summary
    /// delivery `attempt`, after staging the `abandoned` receipt of an
    /// unsettled earlier attempt; then dispatch. With `redispatch`, the
    /// provider refused the turn's primary call as too long: compact
    /// whether due or not, then dispatch that attempt of the call.
    Compact {
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
        redispatch: Option<u32>,
    },
    /// Append the projection of a durable summary unless `appended`, then
    /// dispatch `attempt`.
    Compacted {
        summary: Message,
        keep_from: u64,
        appended: bool,
        attempt: u32,
    },
    /// Append the projection of a durable micro-compaction unless
    /// `appended`, then dispatch `attempt`.
    Cleared {
        before: u64,
        attempt: u32,
        appended: bool,
    },
    /// A forced summary settled without a summary: end the run failed.
    Unshortened,
    /// Dispatch delivery `attempt` of the current turn, after staging the
    /// `abandoned` receipt of an unsettled earlier attempt.
    Dispatch {
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    },
    /// Append the response unless `appended`, then end or plan its tools;
    /// or, with `retry_feedback`, send the feedback for another turn.
    Received {
        message: Message,
        stop_reason: StopReason,
        appended: bool,
        retry_feedback: Option<String>,
    },
    /// Plan and run the tool calls of the last response.
    Batch(Batch),
    /// Append the tool results (or stop feedback) unless `appended`, then
    /// start the next turn.
    Results { message: Message, appended: bool },
    /// Append the results that pair a final tool call unless `appended`,
    /// then end the run as `ending` says.
    Finishing {
        message: Message,
        ending: Box<Ending>,
        appended: bool,
    },
    /// The run was cancelled: pair the session, then end it `aborted`.
    Abort(Unpaired),
}

/// The tool calls of one response, as far as they got.
pub(super) struct Batch {
    /// Every call in order: reused results, or calls to plan.
    pub(super) calls: Vec<ToolRecovery>,
    /// Decisions recorded so far.
    pub(super) decisions: Vec<ToolDecisionRecord>,
    /// Host answers with the input each one covers.
    pub(super) approvals: HashMap<String, RecordedApproval>,
    /// `after_seq` of the batch when it already has durable tool events.
    pub(super) after_seq: Option<u64>,
}

/// One run in progress: its journal, its session view and its accounting.
pub(super) struct Runner {
    pub(super) agent: Arc<AgentInner>,
    pub(super) journal: Journal,
    pub(super) outbox: Outbox,
    pub(super) run: RunIdentity,
    pub(super) cancel: CancellationToken,
    pub(super) snapshot: Arc<ToolSnapshot>,
    /// Whether a skill narrowed this turn's tools.
    pub(super) narrowed: bool,
    pub(super) log: SessionLog,
    /// The session's frozen prefix and what the model was told since;
    /// `None` until the prefix is logged with the session's first prompt.
    pub(super) catalog: Option<Catalog>,
    /// The catalog version the run last diffed against (see
    /// [`crate::catalog::version`]).
    pub(super) seen: Option<(u64, u64, u64)>,
    /// Entries in the stored session log.
    pub(super) session_len: u64,
    pub(super) turn: u32,
    pub(super) usage: Usage,
    pub(super) usage_complete: bool,
    /// Cost of the priced calls so far; `None` until one was priced.
    pub(super) cost: Option<f64>,
    /// Why the last response stopped, for `post_turn`.
    pub(super) stop_reason: StopReason,
    /// When this call of the run started, for the result's `duration_ms`.
    pub(super) started: Instant,
    /// The run's subagent child runs; `None` without subagents.
    pub(super) family: Option<Arc<Family>>,
    /// The budget of the run's tree; `None` without one.
    pub(super) budget: Option<Meter>,
    /// Peer messages logged in an event that is not durable yet.
    pub(super) peers: Option<HeldPeers>,
    /// `stop` retries honoured in this run call (see `runner/hooks.rs`).
    pub(super) final_answer_retries: u32,
    /// Where the run stands on its fallback models and in this turn's
    /// provider retries (see `runner/retry.rs`).
    pub(super) standing: Standing,
    /// The schema the run's final answer must satisfy; `None` takes any
    /// text (see `runner/structured.rs`).
    pub(super) output: Option<Arc<OutputContract>>,
    /// Schema retries the run spent, counted from its durable log on a
    /// resume.
    pub(super) schema_retries: u32,
    /// Verification retries the run spent (see `runner/structured.rs`),
    /// counted from its durable log on a resume.
    pub(super) verify_retries: u32,
    /// Summaries the run began because the provider refused a prompt as
    /// too long (see `runner/compact.rs`), counted from its durable
    /// deliveries on a resume.
    pub(super) forced: u32,
    /// Summary deliveries of the current turn, so the next one takes the
    /// next attempt id.
    pub(super) summary_calls: u32,
    /// Cut answers the run continued (see [`crate::truncation`]), counted
    /// from its durable log on a resume.
    pub(super) truncations: u32,
    /// The cut parts the next text answer continues; its final answer
    /// starts with them.
    pub(super) truncated: String,
    /// What the loop guard counted of the run, read back from its durable
    /// log on a resume.
    pub(super) watch: Watch,
    /// The run's tool choice: its own, else the agent's.
    pub(super) tool_choice: Option<ToolChoice>,
    /// The tool whose call ends the run (see `runner/final_tool.rs`).
    pub(super) final_tool: Option<FinalTool>,
    /// A logged response whose final tool call the run is deciding on,
    /// none of its calls answered yet: feedback for the model and every
    /// terminal pair them first.
    pub(super) unpaired: Option<Unanswered>,
    /// What the run spent retrying tool calls (see
    /// [`crate::tool_retry::RETRY_BUDGET`]); a resumed run starts at zero.
    pub(super) retry_clock: Arc<RetryClock>,
}

impl Runner {
    pub(super) fn new(
        agent: Arc<AgentInner>,
        journal: Journal,
        outbox: Outbox,
        run: RunIdentity,
        cancel: CancellationToken,
        entries: Vec<LogEntry>,
        progress: Progress,
    ) -> Self {
        let session_len = entries.len() as u64;
        let log = session_log(entries);
        let output = agent.output.clone();
        let watch = Watch::new(&agent.spec.loop_guard, agent.snapshot().polls());
        let tool_choice = agent.spec.tool_choice.clone();
        let final_tool = agent.final_tool.clone();
        let mut runner = Self {
            snapshot: agent.snapshot(),
            narrowed: false,
            catalog: Catalog::of(&log),
            seen: None,
            agent,
            journal,
            outbox,
            run,
            cancel,
            log,
            session_len,
            turn: progress.turn,
            usage: progress.usage,
            usage_complete: progress.usage_complete,
            cost: progress.cost,
            stop_reason: progress.stop_reason,
            started: progress.started,
            family: None,
            budget: None,
            peers: None,
            final_answer_retries: 0,
            standing: progress.standing,
            output,
            schema_retries: 0,
            verify_retries: 0,
            forced: 0,
            summary_calls: 0,
            truncations: 0,
            truncated: String::new(),
            watch,
            tool_choice,
            final_tool,
            unpaired: None,
            retry_clock: Arc::default(),
        };
        runner.narrow();
        runner
    }

    /// Take this turn's tools, and return its model, when a skill the
    /// model just loaded sets them (see [`crate::skills`]).
    fn narrow(&mut self) -> Option<String> {
        self.snapshot = self.agent.snapshot();
        self.narrowed = false;
        self.offered_only();
        let set = self.agent.skill_set()?;
        if set.overlays.is_empty() {
            return None;
        }
        let view = self.log.provider_view();
        let overlay = skills::overlay(&view, &set.overlays)?;
        if let Some(allowed) = &overlay.allowed_tools {
            let keep = |name: &str| allowed.iter().any(|tool| tool == name);
            self.snapshot = Arc::new(self.snapshot.filtered(keep));
            self.narrowed = true;
        }
        overlay.model.clone()
    }

    /// Run steps until the run ends or suspends, then settle the peer
    /// messages it still holds.
    pub(super) async fn drive(mut self, step: Step) -> Result<(), RunError> {
        let driven = self.steps(step).await;
        self.settle_peers(true).await;
        driven
    }

    /// Run steps until the run ends or suspends, acking held peer messages
    /// as soon as a step made them durable.
    async fn steps(&mut self, mut step: Step) -> Result<(), RunError> {
        loop {
            let next = match step {
                Step::Prompt {
                    prompt,
                    appended,
                    moved,
                } => {
                    if !appended {
                        self.append_prompt(prompt, moved).await?;
                    }
                    Some(self.turn_start().await)
                }
                Step::Compact {
                    attempt,
                    abandoned,
                    redispatch,
                } => self.compact(attempt, abandoned, redispatch).await?,
                Step::Compacted {
                    summary,
                    keep_from,
                    appended,
                    attempt,
                } => (self.compacted(summary, keep_from, appended, attempt)).await?,
                Step::Cleared {
                    before,
                    attempt,
                    appended,
                } => self.cleared(before, attempt, appended).await?,
                Step::Unshortened => {
                    self.unshortened().await?;
                    None
                }
                Step::Dispatch { attempt, abandoned } => self.dispatch(attempt, abandoned).await?,
                Step::Received {
                    message,
                    stop_reason,
                    appended,
                    retry_feedback,
                } => (self.received(message, stop_reason, appended, retry_feedback)).await?,
                Step::Batch(batch) => self.batch(batch).await?,
                Step::Results { message, appended } => {
                    if !appended {
                        self.append(message).await?;
                    }
                    // The batch's results are logged, so the session stays
                    // paired when the guard ends the run here.
                    match self.watch.tool_trip() {
                        Some(trip) => {
                            self.loop_guard(trip).await?;
                            None
                        }
                        None => {
                            // A cancel that cut it short aborts at the next
                            // turn's start.
                            self.post_turn(self.stop_reason).await;
                            self.turn += 1;
                            self.summary_calls = 0;
                            Some(self.turn_start().await)
                        }
                    }
                }
                Step::Finishing {
                    message,
                    ending,
                    appended,
                } => {
                    self.finishing(message, *ending, appended).await?;
                    None
                }
                Step::Abort(unpaired) => {
                    self.cancelled(unpaired).await?;
                    None
                }
            };
            self.settle_peers(false).await;
            match next {
                Some(next) => step = next,
                None => return Ok(()),
            }
        }
    }

    /// The first step of a turn: fire `turn_start`, then compaction when the
    /// spec turns it on.
    async fn turn_start(&self) -> Step {
        self.turn_start_hook(self.turn).await;
        match self.agent.spec.compaction {
            Some(_) => Step::Compact {
                attempt: 1,
                abandoned: None,
                redispatch: None,
            },
            None => Step::Dispatch {
                attempt: 1,
                abandoned: None,
            },
        }
    }

    /// Stage `event` and count it for the loop guard: the live counts are
    /// those [`Watch::of`] reads back from the durable log.
    pub(super) fn stage_seen(&mut self, event: Event) {
        self.watch.see(&event);
        self.journal.stage(event);
    }

    /// Whether `max_turns` allows no more provider calls.
    pub(super) fn out_of_turns(&self) -> bool {
        (self.agent.spec.max_turns).is_some_and(|max| self.turn >= max)
    }

    /// Why the budget allows no more provider calls; `None` when it does.
    pub(super) fn refusal(&self) -> Option<Refusal> {
        self.budget.as_ref()?.refusal()
    }

    /// One provider call: the `ProviderPending` barrier, the call, and the
    /// `AssistantReceived` write-through. With an `after_provider_call`
    /// chain, the paid receipt and its usage are written first, with
    /// `ProviderPending` re-saved at the totals that count them.
    async fn dispatch(
        &mut self,
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    ) -> Result<Option<Step>, RunError> {
        if let Some(mut receipt) = abandoned {
            self.settle(&mut receipt);
            self.journal.stage(Event::ModelCallReceipt(receipt));
        }
        if self.cancel.is_cancelled() {
            self.abort().await?;
            return Ok(None);
        }
        if let Some(max) = self.agent.spec.max_turns
            && self.turn >= max
        {
            self.max_turns(max).await?;
            return Ok(None);
        }
        if let Some(refusal) = self.refusal() {
            self.over_budget(refusal).await?;
            return Ok(None);
        }
        self.stage_children();
        let model = self.narrow();
        let view = self.log.provider_view();
        let mut messages: Vec<Message> =
            view.iter().map(|message| Message::clone(message)).collect();
        let Some(mut context) = self.context(view).await else {
            self.abort().await?;
            return Ok(None);
        };
        add_context(&mut messages, std::mem::take(&mut context.blocks));
        let mut request = self.request(messages);
        if model.is_some() || self.standing.fallbacks > 0 {
            request.model = self.call_model(model);
        }
        let request = match self.before_provider_call(request, self.turn).await {
            BeforeCall::Send(request) => {
                let mut request = *request;
                // After the hooks: one may have changed the tools.
                if let Some(name) = Self::drop_unoffered_choice(&mut request) {
                    context
                        .metadata
                        .insert("tool_choice_dropped".into(), name.into());
                }
                request
            }
            // Python's `_gate_retry_tail`: the turn ends without a call, its
            // feedback durable before the next turn's. The view ends with a
            // user message here (a resumed response goes to `received`,
            // never back to `dispatch`), so no tool call is left unanswered.
            BeforeCall::ForceContinue(feedback) => {
                // Bounded even with `max_turns` unset: such a turn never
                // reaches the budget, and appends to the session.
                if let Some(trip) = self.watch.idle_trip(self.turn) {
                    self.loop_guard(trip).await?;
                    return Ok(None);
                }
                let message = Message::user_text(skills::system_reminder(&feedback));
                return Ok(Some(self.feedback(message).await?));
            }
            BeforeCall::Blocked(reason) => {
                self.hook_failed(reason).await?;
                return Ok(None);
            }
            BeforeCall::Ended => {
                self.abort().await?;
                return Ok(None);
            }
        };
        // Before anything of the call is staged: a run that waits for its
        // place has dispatched nothing.
        let slot = match self.slot().await? {
            Slot::Cancelled => {
                self.abort().await?;
                return Ok(None);
            }
            held => held,
        };
        self.stage_context(&request, context);
        let call = ModelCall::prepare(&self.run.run_id, self.turn, attempt, &request.model);
        self.stage_seen(Event::ModelCallDelivery(call.delivery().clone()));
        self.save(Phase::ProviderPending { attempt }).await?;
        let ready = self.journal.flush().await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        let call_model = request.model.clone();
        self.provider_call_start(self.turn, &call_model).await;
        let outcome = {
            let (journal, outbox) = (&mut self.journal, &self.outbox);
            let mut on_delta = |delta| {
                let partial = Event::PartialAssistant(delta);
                if let Some(envelope) = journal.stage(partial) {
                    outbox.push(envelope);
                }
            };
            let provider = &*self.agent.provider;
            // Redaction sees whole responses: a match can span two deltas.
            let deltas = self.agent.response_redactor().is_none();
            let on_delta = if deltas {
                Some(&mut on_delta as _)
            } else {
                None
            };
            call.drive(provider, request, &self.cancel, on_delta).await
        };
        drop(slot);
        let mut receipt = outcome.receipt;
        self.settle(&mut receipt);
        let (usage, cost) = (receipt.usage, receipt.cost_usd);
        let served = receipt.actual_model.clone();
        let (disposition, duration_ms) = (receipt.disposition, receipt.duration_ms);
        let empty = matches!(&outcome.result, Ok(assembly) if cut_empty(assembly));
        if empty {
            receipt.disposition = CallDisposition::Rejected;
        }
        self.journal.stage(Event::ModelCallReceipt(receipt));
        // An `after_provider_call` hook may never return (a client callback
        // with no timeout): the paid receipt and its usage are made durable
        // before the hooks run (`provider_call_stop` included), the response
        // only after them.
        let early = outcome.result.is_ok() && !empty && self.checks_responses();
        if early {
            self.stage_usage(usage, cost, served.clone());
            self.save(Phase::ProviderPending { attempt }).await?;
            let ready = self.journal.flush().await?;
            self.event_emit(&ready).await;
            self.outbox.deliver(ready).await;
        }
        let model = served.as_deref().unwrap_or(&call_model);
        (self.provider_call_stop(self.turn, model, disposition, usage, duration_ms)).await;
        let failure = match outcome.result {
            // One terminal write: the paid receipt, usage, error and result.
            Ok(_) if empty => {
                self.stage_usage(usage, cost, served);
                self.output_limit().await?;
                return Ok(None);
            }
            Ok(assembly) => {
                let unstaged = (!early).then_some((cost, served));
                return self.accept(assembly, unstaged).await;
            }
            Err(failure) => failure,
        };
        // A call that failed or was cancelled may still have been paid; one
        // known to have cost nothing reports no usage, as Python's does not.
        if !failure.free {
            self.stage_usage(usage, cost, served);
        }
        if matches!(failure.error, ProviderError::Cancelled) {
            self.abort().await?;
            return Ok(None);
        }
        // A prompt too long for the model: shorten the view and call again,
        // which is not a provider retry.
        if matches!(failure.error, ProviderError::ContextLength { .. })
            && let Some(next) = self.overflow(attempt).await?
        {
            return Ok(Some(next));
        }
        match self.recover(&failure, attempt, &call_model).await? {
            Recovered::Call(next) => return Ok(Some(next)),
            Recovered::Ended => return Ok(None),
            Recovered::Failed => {}
        }
        // One terminal write: receipt, usage, error, status and result.
        self.journal.stage(Event::Error(failure.event));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await?;
        Ok(None)
    }

    /// Run `after_provider_call`, then stage the response, and its `Usage`
    /// when `unstaged` holds the call's cost and the model that served it
    /// (`None`: the receipt's write already carried it), and write them
    /// through with `AssistantReceived`, which also records a hook's retry:
    /// the response and what to do with it are durable in one write.
    async fn accept(
        &mut self,
        assembly: AssistantAssembly,
        unstaged: Option<(Option<f64>, Option<String>)>,
    ) -> Result<Option<Step>, RunError> {
        let (assembly, retry_feedback) = match self.after_provider_call(assembly, self.turn).await {
            AfterCall::Send(assembly) => (*assembly, None),
            AfterCall::Retry(assembly, feedback) => (*assembly, Some(feedback)),
            AfterCall::Blocked(reason) => {
                self.hook_failed(reason).await?;
                return Ok(None);
            }
            AfterCall::Ended => {
                self.abort().await?;
                return Ok(None);
            }
        };
        let AssistantAssembly {
            message,
            stop_reason,
            usage,
            ..
        } = assembly;
        // The turn's call is answered: the next turn has its own retries.
        self.standing.retries = 0;
        self.standing.failed_over = false;
        self.stage_seen(Event::Assistant(AssistantEvent {
            message: message.clone(),
            stop_reason,
        }));
        if let Some((cost, served)) = unstaged {
            self.stage_usage(usage, cost, served);
        }
        let phase = Phase::AssistantReceived {
            message: message.clone(),
            stop_reason,
            retry_feedback: retry_feedback.clone(),
        };
        self.save(phase).await?;
        Ok(Some(Step::Received {
            message,
            stop_reason,
            appended: false,
            retry_feedback,
        }))
    }

    async fn received(
        &mut self,
        message: Message,
        stop_reason: StopReason,
        appended: bool,
        retry_feedback: Option<String>,
    ) -> Result<Option<Step>, RunError> {
        let calls: Vec<ToolRecovery> = message
            .tool_uses()
            .cloned()
            .map(ToolRecovery::Replan)
            .collect();
        let text = message.text();
        self.stop_reason = stop_reason;
        let retry = retry_feedback.map(|feedback| retry_message(&message, &feedback));
        let final_call = (self.final_tool.as_ref())
            .is_some_and(|tool| message.tool_uses().any(|call| call.name == tool.name))
            .then(|| message.clone());
        if !appended {
            self.append(message).await?;
        }
        if let Some(retry) = retry {
            return Ok(Some(self.feedback(retry).await?));
        }
        // The parts the output limit cut off before this response.
        let parts = std::mem::take(&mut self.truncated);
        if let Some(response) = final_call {
            return self.final_call(response, stop_reason).await;
        }
        if calls.is_empty() {
            if let Some(reminder) = self.continuing(stop_reason, &text) {
                self.truncated = parts + &text;
                self.truncations += 1;
                return Ok(Some(self.feedback(Message::user_text(reminder)).await?));
            }
            let stitched = !parts.is_empty();
            let mut text = parts + &text;
            // Each part was scrubbed as a response; a match the cut split
            // across two parts is whole only here.
            if stitched && let Some(redactor) = self.agent.response_redactor() {
                text = redactor.text(&text).into_owned();
            }
            if let Some(next) = self.hear_back().await? {
                return Ok(next);
            }
            let value = match self.check_answer(&text).await? {
                Checked::Valid(value) => value,
                Checked::Retry(feedback) => return Ok(Some(feedback)),
                Checked::Ended => return Ok(None),
            };
            let answer = match self.stop(&text, value.as_ref()).await? {
                Stopping::Stop { text } => text,
                Stopping::Continue(feedback) => return Ok(Some(feedback)),
                Stopping::Ended => return Ok(None),
            };
            let value = match self.check_rewrite(&text, &answer, value).await? {
                Checked::Valid(value) => value,
                Checked::Retry(feedback) => return Ok(Some(feedback)),
                Checked::Ended => return Ok(None),
            };
            if !self.post_turn(stop_reason).await {
                self.abort().await?;
                return Ok(None);
            }
            self.complete(answer, stop_reason, value).await?;
            return Ok(None);
        }
        Ok(Some(Step::Batch(Batch {
            calls,
            decisions: Vec::new(),
            approvals: HashMap::new(),
            after_seq: None,
        })))
    }

    /// The feedback that continues a text answer with `text` that stopped
    /// for `stop_reason`, when it is cut off and the spec's truncation
    /// recovery has an attempt left; `None` takes it as the answer.
    fn continuing(&self, stop_reason: StopReason, text: &str) -> Option<String> {
        let spec = self.agent.spec.truncation_recovery.as_ref()?;
        let left = self.truncations < spec.max_attempts;
        (left && crate::truncation::cut(stop_reason, text)).then(|| spec.reminder())
    }
}
