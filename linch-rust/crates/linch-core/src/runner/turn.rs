//! [`Runner`]: one run's state and the provider half of a turn.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::Outbox;
use crate::agent::{AgentInner, RunError};
use crate::events::Event;
use crate::events::kinds::{
    AssistantEvent, ErrorEvent, LoopGuardAction, LoopGuardEvent, LoopGuardReason, ResultEvent,
    ResultSubtype, UsageEvent,
};
use crate::events::model_call::ModelCallReceiptEvent;
use crate::journal::{Journal, ToolRecovery};
use crate::message::Message;
use crate::provider::call::ModelCall;
use crate::provider::{AssistantAssembly, ProviderError};
use crate::registry::ToolSnapshot;
use crate::run::{Checkpoint, Phase, RunStatus, StopReason, ToolDecisionRecord};
use crate::scheduler::{RecordedApproval, RunIdentity};
use crate::session_log::{LogEntry, MessageEntry, SessionLog};
use crate::usage::Usage;

/// What the run does next.
pub(super) enum Step {
    /// Append the prompt unless `appended`, then dispatch.
    Prompt { prompt: Message, appended: bool },
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
    /// Append the tool results unless `appended`, then start the next turn.
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

/// Turn index and usage carried over from a checkpoint.
#[derive(Clone, Copy)]
pub(super) struct Progress {
    pub(super) turn: u32,
    pub(super) usage: Usage,
    pub(super) usage_complete: bool,
}

impl Progress {
    /// A new run: turn 0, nothing spent.
    pub(super) const FIRST: Self = Self {
        turn: 0,
        usage: Usage {
            input_tokens: 0,
            output_tokens: 0,
            cache_read_tokens: 0,
            cache_creation_tokens: 0,
        },
        usage_complete: true,
    };

    pub(super) fn of(checkpoint: &Checkpoint) -> Self {
        Self {
            turn: checkpoint.turn,
            usage: checkpoint.usage,
            usage_complete: checkpoint.usage_complete,
        }
    }
}

/// One run in progress: its journal, its session view and its accounting.
pub(super) struct Runner {
    pub(super) agent: Arc<AgentInner>,
    pub(super) journal: Journal,
    pub(super) outbox: Outbox,
    pub(super) run: RunIdentity,
    pub(super) cancel: CancellationToken,
    pub(super) snapshot: Arc<ToolSnapshot>,
    log: SessionLog,
    /// Entries in the stored session log.
    session_len: u64,
    turn: u32,
    usage: Usage,
    usage_complete: bool,
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
        Self {
            snapshot: agent.tools.snapshot(),
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
        }
    }

    /// Run steps until the run ends or suspends.
    pub(super) async fn drive(mut self, mut step: Step) -> Result<(), RunError> {
        loop {
            let next = match step {
                Step::Prompt { prompt, appended } => {
                    if !appended {
                        self.append(prompt).await?;
                    }
                    Some(Step::Dispatch {
                        attempt: 1,
                        abandoned: None,
                    })
                }
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
                    self.turn += 1;
                    Some(Step::Dispatch {
                        attempt: 1,
                        abandoned: None,
                    })
                }
            };
            match next {
                Some(next) => step = next,
                None => return Ok(()),
            }
        }
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
        self.snapshot = self.agent.tools.snapshot();
        let view = self.log.provider_view();
        let messages = view.iter().map(|message| Message::clone(message)).collect();
        let request = (self.agent.spec).request(self.snapshot.definitions(), messages);
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
        self.settle(&outcome.receipt);
        self.journal.stage(Event::ModelCallReceipt(outcome.receipt));
        match outcome.result {
            Ok(assembly) => self.accept(assembly).await.map(Some),
            Err(failure) if matches!(failure.error, ProviderError::Cancelled) => {
                self.abort().await?;
                Ok(None)
            }
            Err(failure) => {
                self.journal.stage(Event::Error(failure.event));
                let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
                self.end(RunStatus::Failed, subtype, stop, None).await?;
                Ok(None)
            }
        }
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
        if let Some(usage) = usage {
            let cumulative = self.usage;
            self.journal
                .stage(Event::Usage(UsageEvent { usage, cumulative }));
        }
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
        if !appended {
            self.append(message).await?;
        }
        if calls.is_empty() {
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

    /// Session write: flush the held checkpoint, then append `message`.
    async fn append(&mut self, message: Message) -> Result<(), RunError> {
        let ready = self.journal.flush_checkpoint().await?;
        self.outbox.deliver(ready).await;
        let entry = LogEntry::Message(MessageEntry {
            message: Arc::new(message.clone()),
            visible: true,
            historical: true,
        });
        let (sessions, namespace) = (&self.agent.sessions, &self.agent.namespace);
        let session = &self.run.session_id;
        self.session_len =
            (sessions.append(namespace, session, self.session_len, vec![entry])).await?;
        self.log.append(message, true, true);
        Ok(())
    }

    /// Save a checkpoint for `phase` and deliver what a write-through made
    /// durable.
    pub(super) async fn save(&mut self, phase: Phase) -> Result<(), RunError> {
        let checkpoint = Checkpoint {
            turn: self.turn,
            usage: self.usage,
            usage_complete: self.usage_complete,
            session_len: self.session_len,
            phase,
        };
        let ready = self.journal.save(checkpoint).await?;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// Count a settled receipt: known usage adds up, unknown usage makes
    /// the total incomplete (never a zero fill).
    fn settle(&mut self, receipt: &ModelCallReceiptEvent) {
        match receipt.usage {
            Some(usage) => self.usage += usage,
            None => self.usage_complete = false,
        }
    }

    /// End the run with `status` and its `Result`.
    pub(super) async fn end(
        &mut self,
        status: RunStatus,
        subtype: ResultSubtype,
        stop_reason: StopReason,
        final_text: Option<String>,
    ) -> Result<(), RunError> {
        let result = ResultEvent {
            subtype,
            stop_reason,
            total_usage: self.usage,
            usage_complete: self.usage_complete,
            final_text,
            structured_output: None,
            structured_error: None,
            trace_id: None,
        };
        let ready = self.journal.finish(status, result).await?;
        self.outbox.deliver(ready).await;
        Ok(())
    }

    /// The abort terminal. Python reports `stop_reason: error` with it.
    async fn abort(&mut self) -> Result<(), RunError> {
        let (subtype, stop) = (ResultSubtype::Aborted, StopReason::Error);
        self.end(RunStatus::Aborted, subtype, stop, None).await
    }

    /// The `max_turns` terminal, as Python's `_max_turns_tail` emits it.
    async fn max_turns(&mut self, max: u32) -> Result<(), RunError> {
        self.journal.stage(Event::LoopGuard(LoopGuardEvent {
            reason: LoopGuardReason::MaxTurns,
            detail: format!("Maximum turns ({max}) reached."),
            action: LoopGuardAction::Stop,
        }));
        let error = json!({
            "name": "TurnLimitError",
            "message": "max turns exceeded",
            "retryable": false,
        });
        let error = error.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent { error }));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await
    }
}
