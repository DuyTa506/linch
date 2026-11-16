//! The schema gate on a run's final answer (see [`crate::structured`]).
//!
//! A text-only answer is checked after any background subagent was heard
//! back from and before the `stop` chain, which sees the parsed value. A
//! failure stages a `verification` event (`verifier: output_schema`) and,
//! while the agent's `structured_output_retries` allow, goes back to the
//! model as feedback in a system reminder, through the same held
//! `ToolResults` step as a `stop` hook's feedback: the event, the
//! notification and the checkpoint commit together before the session is
//! written. The retries spent are counted from those durable events, so a
//! resumed run replays a retry it had not made durable, never one it had.
//! Past the bound the run ends failed with a `StructuredOutputError`.
//!
//! A `stop` hook's rewrite of the answer is checked again; one that breaks
//! the schema fails the run without going back to the model, since the
//! model did not write it.

use serde_json::json;

use super::end::Answer;
use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ErrorEvent, ResultSubtype, VerificationAction, VerificationEvent};
use crate::json::JsonObject;
use crate::message::Message;
use crate::run::{RunStatus, StopReason};
use crate::skills::system_reminder;
use crate::structured::{VERIFIER, feedback};

/// What the schema gate made of an answer.
pub(super) enum Checked {
    /// Go on with this value: the parsed answer, or `None` when the run
    /// has no schema.
    Valid(Option<JsonObject>),
    /// The answer went back to the model; this step is next.
    Retry(Step),
    /// The run ended failed.
    Ended,
}

impl Runner {
    /// Check the text-only answer `text` against the run's schema.
    pub(super) async fn check_answer(&mut self, text: &str) -> Result<Checked, RunError> {
        let Some(output) = self.output.clone() else {
            return Ok(Checked::Valid(None));
        };
        let error = match output.check(text) {
            Ok(value) => return Ok(Checked::Valid(Some(value))),
            Err(error) => error,
        };
        let allowed = self.agent.spec.structured_output_retries;
        if self.schema_retries < allowed {
            self.schema_retries += 1;
            self.stage_verification(VerificationAction::Retry, &error);
            let message = Message::user_text(system_reminder(&feedback(&error)));
            return Ok(Checked::Retry(self.feedback(message).await?));
        }
        let why = match allowed {
            0 => "the final answer does not satisfy the output schema".to_owned(),
            n => format!(
                "the final answer still does not satisfy the output schema after {n} retries"
            ),
        };
        self.schema_failed(text, why, error).await?;
        Ok(Checked::Ended)
    }

    /// Check `text`, the final answer after the `stop` chain, again when a
    /// hook rewrote `answer` into it; `value` is what `answer` parsed to.
    pub(super) async fn check_rewrite(
        &mut self,
        answer: &str,
        text: &str,
        value: Option<JsonObject>,
    ) -> Result<Checked, RunError> {
        let Some(output) = self.output.clone() else {
            return Ok(Checked::Valid(None));
        };
        if text == answer {
            return Ok(Checked::Valid(value));
        }
        match output.check(text) {
            Ok(value) => Ok(Checked::Valid(Some(value))),
            Err(error) => {
                let why = "a stop hook rewrote the final answer into one that does not satisfy \
                           the output schema";
                self.schema_failed(text, why.to_owned(), error).await?;
                Ok(Checked::Ended)
            }
        }
    }

    /// End the run completed with `text` and its checked `value`.
    pub(super) async fn complete(
        &mut self,
        text: String,
        stop_reason: StopReason,
        value: Option<JsonObject>,
    ) -> Result<(), RunError> {
        let answer = Answer {
            text: (!text.is_empty()).then_some(text),
            structured_output: value,
            structured_error: None,
        };
        let (status, subtype) = (RunStatus::Completed, ResultSubtype::Success);
        self.end_with(status, subtype, stop_reason, answer).await
    }

    /// The terminal for an answer the schema gate gave up on: the
    /// `verification` stop, the `StructuredOutputError` and the failed
    /// `Result`, in one write.
    async fn schema_failed(
        &mut self,
        text: &str,
        why: String,
        error: String,
    ) -> Result<(), RunError> {
        self.stage_verification(VerificationAction::Stop, &error);
        let message = format!("{why}: {error}");
        let event =
            json!({"name": "StructuredOutputError", "message": message, "retryable": false});
        let error_object = event.as_object().cloned().unwrap_or_default();
        self.journal.stage(Event::Error(ErrorEvent {
            error: error_object,
        }));
        let answer = Answer {
            text: (!text.is_empty()).then(|| text.to_owned()),
            structured_output: None,
            structured_error: Some(error),
        };
        let (status, subtype) = (RunStatus::Failed, ResultSubtype::Error);
        self.end_with(status, subtype, StopReason::Error, answer)
            .await
    }

    fn stage_verification(&mut self, action: VerificationAction, error: &str) {
        self.journal.stage(Event::Verification(VerificationEvent {
            verifier: VERIFIER.to_owned(),
            action,
            feedback: error.to_owned(),
            attempt: u64::from(self.schema_retries),
        }));
    }
}
