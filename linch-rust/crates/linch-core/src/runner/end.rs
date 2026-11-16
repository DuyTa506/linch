//! The run's terminals: completed, failed, aborted, the turn limit and
//! the budget.

use std::time::Duration;

use serde_json::json;

use super::turn::Runner;
use crate::agent::RunError;
use crate::budget::Refusal;
use crate::events::Event;
use crate::events::kinds::{
    ChildRunStatusEvent, ErrorEvent, LoopGuardAction, LoopGuardEvent, LoopGuardReason, ResultEvent,
    ResultSubtype,
};
use crate::json::JsonObject;
use crate::provider::AssistantAssembly;
use crate::run::{RunStatus, StopReason};

/// What the `Result` of a run reports as its answer.
#[derive(Default)]
pub(super) struct Answer {
    /// The final-answer text.
    pub(super) text: Option<String>,
    /// The answer parsed and checked against the run's output schema.
    pub(super) structured_output: Option<JsonObject>,
    /// Why the answer did not satisfy the output schema.
    pub(super) structured_error: Option<String>,
}

impl Runner {
    /// [`Self::end_with`] an answer of `final_text` alone.
    pub(super) async fn end(
        &mut self,
        status: RunStatus,
        subtype: ResultSubtype,
        stop_reason: StopReason,
        final_text: Option<String>,
    ) -> Result<(), RunError> {
        let answer = Answer {
            text: final_text,
            ..Answer::default()
        };
        self.end_with(status, subtype, stop_reason, answer).await
    }

    /// End the run with `status` and its `Result`, reporting `answer`,
    /// once every child run still running was cancelled and ended. The
    /// `child_run_status` end of each background child not heard of yet
    /// goes into the same write, with no notification.
    pub(super) async fn end_with(
        &mut self,
        status: RunStatus,
        subtype: ResultSubtype,
        stop_reason: StopReason,
        answer: Answer,
    ) -> Result<(), RunError> {
        if let Some(family) = &self.family {
            family.cancel_all();
            family.idle().await;
            for completion in family.take_done() {
                self.journal
                    .stage(Event::ChildRunStatus(ChildRunStatusEvent {
                        child_run_id: completion.child,
                        status: completion.status,
                    }));
            }
        }
        self.stage_children();
        let result = ResultEvent {
            subtype,
            stop_reason,
            total_usage: self.usage,
            usage_complete: self.usage_complete,
            duration_ms: millis(self.started.elapsed()),
            total_cost_usd: self.cost,
            final_text: answer.text,
            structured_output: answer.structured_output,
            structured_error: answer.structured_error,
            trace_id: self.run.trace_id.clone(),
        };
        let ready = self.journal.finish(status, result).await?;
        self.event_emit(&ready).await;
        self.outbox.deliver(ready).await;
        self.agent_stop(status, stop_reason).await;
        Ok(())
    }

    /// The abort terminal. Python reports `stop_reason: error` with it.
    pub(super) async fn abort(&mut self) -> Result<(), RunError> {
        let (subtype, stop) = (ResultSubtype::Aborted, StopReason::Error);
        self.end(RunStatus::Aborted, subtype, stop, None).await
    }

    /// The terminal for a hook that failed closed where the run cannot go
    /// on unchecked (`stop`).
    pub(super) async fn hook_failed(&mut self, reason: String) -> Result<(), RunError> {
        let error = json!({"name": "HookError", "message": reason, "retryable": false});
        let error = error.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent { error }));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await
    }

    /// The terminal for a response the output limit cut off before it held
    /// any text or whole tool call (see [`cut_empty`]). Nothing goes into
    /// the session: there is no answer to log.
    pub(super) async fn output_limit(&mut self) -> Result<(), RunError> {
        let error = json!({
            "name": "OutputLimitError",
            "message": "the response reached the output token limit before it held any text \
                        or complete tool call; raise max_output_tokens",
            "retryable": false,
        });
        let error = error.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent { error }));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::MaxTokens);
        self.end(RunStatus::Failed, subtype, stop, None).await
    }

    /// The terminal for a prompt the provider refused as longer than the
    /// model's context window, when the summary that refusal forced gave
    /// none: calling again would send the refused prompt.
    pub(super) async fn unshortened(&mut self) -> Result<(), RunError> {
        let error = json!({
            "name": "ProviderError",
            "kind": "context_length",
            "message": "the prompt exceeds the model's context window, and the compaction \
                        it forced could not shorten it",
            "retryable": false,
        });
        let error = error.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent { error }));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await
    }

    /// The `max_turns` terminal, as Python's `_max_turns_tail` emits it.
    pub(super) async fn max_turns(&mut self, max: u32) -> Result<(), RunError> {
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

    /// The terminal for a budget that admits no more calls, as Python's
    /// `_budget_exhausted_tail` emits it: the `exceeded` event when a cap
    /// was reached, then the error and a failed result. A capped budget
    /// that cannot count a call of its tree fails with a
    /// `BudgetAccountingError` and no `budget` event.
    pub(super) async fn over_budget(&mut self, refusal: Refusal) -> Result<(), RunError> {
        if let Refusal::Exceeded(event) = &refusal {
            self.journal.stage(Event::Budget(event.clone()));
        }
        self.journal.stage(Event::Error(refusal.error()));
        let (subtype, stop) = (ResultSubtype::Error, StopReason::Error);
        self.end(RunStatus::Failed, subtype, stop, None).await
    }
}

/// `elapsed` in whole milliseconds, saturating.
pub(super) fn millis(elapsed: Duration) -> u64 {
    u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
}

/// Whether `assembly` stopped at the output limit with no text and no tool
/// call: the cut call was dropped, or the model only reasoned. Appending
/// it would log an answer that says nothing (and a reasoning item with
/// nothing after it, which some providers refuse on replay).
pub(super) fn cut_empty(assembly: &AssistantAssembly) -> bool {
    let message = &assembly.message;
    assembly.stop_reason == StopReason::MaxTokens
        && message.text().is_empty()
        && message.tool_uses().next().is_none()
}
