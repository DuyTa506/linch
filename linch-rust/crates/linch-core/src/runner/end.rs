//! The run's terminals: completed, failed, aborted and the turn limit.

use serde_json::json;

use super::turn::Runner;
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{
    ErrorEvent, LoopGuardAction, LoopGuardEvent, LoopGuardReason, ResultEvent, ResultSubtype,
};
use crate::run::{RunStatus, StopReason};

impl Runner {
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
}
