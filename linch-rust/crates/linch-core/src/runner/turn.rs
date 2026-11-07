//! [`Runner`]: one run's state and the provider half of a turn.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio_util::sync::CancellationToken;

use super::end::cut_empty;
use super::hooks::{Stopping, add_context};
use super::inbox::HeldPeers;
use super::progress::Progress;
use super::write::session_log;
use super::{Family, Outbox};
use crate::agent::{AgentInner, RunError};
use crate::budget::{Meter, Refusal};
use crate::catalog::Catalog;
use crate::events::Event;
use crate::events::kinds::{AssistantEvent, ResultSubtype};
use crate::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use crate::journal::{Journal, ToolRecovery};
use crate::message::Message;
use crate::provider::call::ModelCall;
use crate::provider::{AssistantAssembly, ProviderError};
use crate::registry::ToolSnapshot;
use crate::run::{Phase, RunStatus, StopReason, ToolDecisionRecord};
use crate::scheduler::{RecordedApproval, RunIdentity};
use crate::session_log::{LogEntry, SessionLog};
use crate::skills;
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
    /// unsettled earlier attempt; then dispatch.
    Compact {
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    },
    /// Append the projection of a durable summary unless `appended`, then
    /// dispatch.
    Compacted {
        summary: Message,
        keep_from: u64,
        appended: bool,
    },
    /// Dispatch delivery `attempt` of the current turn, after staging the
    /// `abandoned` receipt of an unsettled earlier attempt.
    Dispatch {
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    },
    /// Append the response unless `appended`, then end or plan its tools.
    Received {
        message: Message,
        stop_reason: StopReason,
        appended: bool,
    },
    /// Plan and run the tool calls of the last response.
    Batch(Batch),
    /// Append the tool results (or stop feedback) unless `appended`, then
    /// start the next turn.
    Results { message: Message, appended: bool },
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
                    Some(self.turn_start())
                }
                Step::Compact { attempt, abandoned } => self.compact(attempt, abandoned).await?,
                Step::Compacted {
                    summary,
                    keep_from,
                    appended,
                } => self.compacted(summary, keep_from, appended).await?,
                Step::Dispatch { attempt, abandoned } => self.dispatch(attempt, abandoned).await?,
                Step::Received {
                    message,
                    stop_reason,
                    appended,
                } => self.received(message, stop_reason, appended).await?,
                Step::Batch(batch) => self.batch(batch).await?,
                Step::Results { message, appended } => {
                    if !appended {
                        self.append(message).await?;
                    }
                    // A cancel that cut it short aborts at the next turn's
                    // start.
                    self.post_turn(self.stop_reason).await;
                    self.turn += 1;
                    Some(self.turn_start())
                }
            };
            self.settle_peers(false).await;
            match next {
                Some(next) => step = next,
                None => return Ok(()),
            }
        }
    }

    /// The first step of a turn: compaction when the spec turns it on.
    fn turn_start(&self) -> Step {
        match self.agent.spec.compaction {
            Some(_) => Step::Compact {
                attempt: 1,
                abandoned: None,
            },
            None => Step::Dispatch {
                attempt: 1,
                abandoned: None,
            },
        }
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
    /// `AssistantReceived` write-through.
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
        let Some(context) = self.context(view).await else {
            self.abort().await?;
            return Ok(None);
        };
        add_context(&mut messages, context.blocks);
        let mut request = self.request(messages);
        if let Some(model) = model {
            request.model = skills::model_override(&model, &request.model);
        }
        self.stage_context(&request, context.metadata);
        let call = ModelCall::prepare(&self.run.run_id, self.turn, attempt, &request.model);
        self.journal
            .stage(Event::ModelCallDelivery(call.delivery().clone()));
        self.save(Phase::ProviderPending { attempt }).await?;
        let ready = self.journal.flush().await?;
        self.outbox.deliver(ready).await;
        let outcome = {
            let (journal, outbox) = (&mut self.journal, &self.outbox);
            let mut on_delta = |delta| {
                let partial = Event::PartialAssistant(delta);
                if let Some(envelope) = journal.stage(partial) {
                    outbox.push(envelope);
                }
            };
            let provider = &*self.agent.provider;
            call.drive(provider, request, &self.cancel, Some(&mut on_delta))
                .await
        };
        let mut receipt = outcome.receipt;
        self.settle(&mut receipt);
        let (usage, cost) = (receipt.usage, receipt.cost_usd);
        let served = receipt.actual_model.clone();
        let empty = matches!(&outcome.result, Ok(assembly) if cut_empty(assembly));
        if empty {
            receipt.disposition = CallDisposition::Rejected;
        }
        self.journal.stage(Event::ModelCallReceipt(receipt));
        let failure = match outcome.result {
            // One terminal write: the paid receipt, usage, error and result.
            Ok(_) if empty => {
                self.stage_usage(usage, cost, served);
                self.output_limit().await?;
                return Ok(None);
            }
            Ok(assembly) => return self.accept(assembly, cost, served).await.map(Some),
            Err(failure) => failure,
        };
        // A call that failed or was cancelled may still have been paid.
        self.stage_usage(usage, cost, served);
        if matches!(failure.error, ProviderError::Cancelled) {
            self.abort().await?;
            return Ok(None);
        }
        // One terminal write: receipt, usage, error, status and result.
        self.journal.stage(Event::Error(failure.event));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await?;
        Ok(None)
    }

    /// Stage the response and its `Usage` (with the call's `cost` and the
    /// model that `served` it), and write them through with
    /// `AssistantReceived`.
    async fn accept(
        &mut self,
        assembly: AssistantAssembly,
        cost: Option<f64>,
        served: Option<String>,
    ) -> Result<Step, RunError> {
        let AssistantAssembly {
            message,
            stop_reason,
            usage,
            ..
        } = assembly;
        self.journal.stage(Event::Assistant(AssistantEvent {
            message: message.clone(),
            stop_reason,
        }));
        self.stage_usage(usage, cost, served);
        let phase = Phase::AssistantReceived {
            message: message.clone(),
            stop_reason,
        };
        self.save(phase).await?;
        Ok(Step::Received {
            message,
            stop_reason,
            appended: false,
        })
    }

    async fn received(
        &mut self,
        message: Message,
        stop_reason: StopReason,
        appended: bool,
    ) -> Result<Option<Step>, RunError> {
        let calls: Vec<ToolRecovery> = message
            .tool_uses()
            .cloned()
            .map(ToolRecovery::Replan)
            .collect();
        let text = message.text();
        self.stop_reason = stop_reason;
        if !appended {
            self.append(message).await?;
        }
        if calls.is_empty() {
            if let Some(next) = self.hear_back().await? {
                return Ok(next);
            }
            match self.stop(&text).await? {
                Stopping::Stop => {}
                Stopping::Continue(feedback) => return Ok(Some(feedback)),
                Stopping::Ended => return Ok(None),
            }
            if !self.post_turn(stop_reason).await {
                self.abort().await?;
                return Ok(None);
            }
            let final_text = (!text.is_empty()).then_some(text);
            let status = RunStatus::Completed;
            (self.end(status, ResultSubtype::Success, stop_reason, final_text)).await?;
            return Ok(None);
        }
        Ok(Some(Step::Batch(Batch {
            calls,
            decisions: Vec::new(),
            approvals: HashMap::new(),
            after_seq: None,
        })))
    }
}
