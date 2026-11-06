//! [`Runner`]: one run's state and the provider half of a turn.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio_util::sync::CancellationToken;

use super::Outbox;
use super::end::cut_empty;
use super::hooks::{Stopping, add_context};
use super::progress::Progress;
use crate::agent::{AgentInner, RunError};
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

/// The in-memory log of the stored session `entries`.
pub(super) fn session_log(entries: Vec<LogEntry>) -> SessionLog {
    let mut log = SessionLog::new();
    for entry in entries {
        match entry {
            LogEntry::Message(entry) => {
                let message = Arc::unwrap_or_clone(entry.message);
                log.append(message, entry.visible, entry.historical);
            }
            LogEntry::Projection(entry) => {
                log.record_projection(entry.replacement, entry.reason);
            }
        }
    }
    log
}

/// One run in progress: its journal, its session view and its accounting.
pub(super) struct Runner {
    pub(super) agent: Arc<AgentInner>,
    pub(super) journal: Journal,
    pub(super) outbox: Outbox,
    pub(super) run: RunIdentity,
    pub(super) cancel: CancellationToken,
    pub(super) snapshot: Arc<ToolSnapshot>,
    pub(super) log: SessionLog,
    /// Entries in the stored session log.
    pub(super) session_len: u64,
    pub(super) turn: u32,
    pub(super) usage: Usage,
    pub(super) usage_complete: bool,
    /// Why the last response stopped, for `post_turn`.
    pub(super) stop_reason: StopReason,
    /// When this call of the run started, for the result's `duration_ms`.
    pub(super) started: Instant,
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
        Self {
            snapshot: agent.snapshot(),
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
            stop_reason: progress.stop_reason,
            started: progress.started,
        }
    }

    /// Run steps until the run ends or suspends.
    pub(super) async fn drive(mut self, mut step: Step) -> Result<(), RunError> {
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

    /// One provider call: the `ProviderPending` barrier, the call, and the
    /// `AssistantReceived` write-through.
    async fn dispatch(
        &mut self,
        attempt: u32,
        abandoned: Option<ModelCallReceiptEvent>,
    ) -> Result<Option<Step>, RunError> {
        if let Some(receipt) = abandoned {
            self.settle(&receipt);
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
        self.snapshot = self.agent.snapshot();
        let view = self.log.provider_view();
        let mut messages: Vec<Message> =
            view.iter().map(|message| Message::clone(message)).collect();
        let Some(context) = self.context(view).await else {
            self.abort().await?;
            return Ok(None);
        };
        add_context(&mut messages, context);
        let request = self.agent.request(self.snapshot.definitions(), messages);
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
        self.settle(&receipt);
        let usage = receipt.usage;
        let empty = matches!(&outcome.result, Ok(assembly) if cut_empty(assembly));
        if empty {
            receipt.disposition = CallDisposition::Rejected;
        }
        self.journal.stage(Event::ModelCallReceipt(receipt));
        let failure = match outcome.result {
            // One terminal write: the paid receipt, usage, error and result.
            Ok(_) if empty => {
                self.stage_usage(usage);
                self.output_limit().await?;
                return Ok(None);
            }
            Ok(assembly) => return self.accept(assembly).await.map(Some),
            Err(failure) => failure,
        };
        // A call that failed or was cancelled may still have been paid.
        self.stage_usage(usage);
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

    async fn accept(&mut self, assembly: AssistantAssembly) -> Result<Step, RunError> {
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
        self.stage_usage(usage);
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
