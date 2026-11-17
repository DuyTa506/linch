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
//!
//! An answer that satisfies the schema then meets the agent's verifiers
//! (see [`crate::verification`]), through the same steps and events, but
//! with their own budget: `verification.max_retries`, shared with the
//! `stop` hooks' retries and counted from the durable `verification`
//! events of every verifier but `output_schema`. A `retry` verifier past
//! that budget accepts the answer with an `exhausted` event, as Python's
//! `FinalAnswerVerifierHook` does, where the schema gate fails the run.

use serde_json::json;

use super::end::Answer;
use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ErrorEvent, ResultSubtype, VerificationAction, VerificationEvent};
use crate::json::JsonObject;
use crate::run::{RunStatus, StopReason};
use crate::structured::{VERIFIER, feedback};
use crate::verification::OnFail;

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
    /// Check the text-only answer `text` against the policy's completion
    /// gates, then the run's schema, then the agent's verifiers.
    pub(super) async fn check_answer(&mut self, text: &str) -> Result<Checked, RunError> {
        if let Some(checked) = self.gate_answer(text, &[]).await? {
            return Ok(checked);
        }
        let value = match self.output.clone() {
            None => None,
            Some(output) => match output.check(text) {
                Ok(value) => Some(value),
                Err(error) => return self.schema_retry(text, error).await,
            },
        };
        self.verify(text, value).await
    }

    /// Send a schema `error` back to the model, or end the run once the
    /// retries are spent.
    pub(super) async fn schema_retry(
        &mut self,
        text: &str,
        error: String,
    ) -> Result<Checked, RunError> {
        let allowed = self.agent.spec.structured_output_retries;
        let tool = self.final_tool.as_ref().map(|tool| tool.name.clone());
        let feedback = feedback(&error, tool.as_deref());
        let why = match allowed {
            0 => "the final answer does not satisfy the output schema".to_owned(),
            n => format!(
                "the final answer still does not satisfy the output schema after {n} retries"
            ),
        };
        self.retry_or_fail(text, error, feedback, why).await
    }

    /// Send `feedback` back to the model as a retry of the schema gate's
    /// budget (`structured_output_retries`, counted from the durable
    /// `verification` events), or end the run failed for `why` once it is
    /// spent. A refused final tool call uses it too, so a hook or a rule
    /// that always refuses cannot keep the run going.
    pub(super) async fn retry_or_fail(
        &mut self,
        text: &str,
        error: String,
        feedback: String,
        why: String,
    ) -> Result<Checked, RunError> {
        if self.schema_retries < self.agent.spec.structured_output_retries {
            self.schema_retries += 1;
            self.stage_verification(VERIFIER, VerificationAction::Retry, &error);
            return Ok(Checked::Retry(self.send_back(&feedback).await?));
        }
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
    pub(super) async fn schema_failed(
        &mut self,
        text: &str,
        why: String,
        error: String,
    ) -> Result<(), RunError> {
        self.stage_verification(VERIFIER, VerificationAction::Stop, &error);
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

    /// Run the agent's verifiers on `text`, which passed the schema as
    /// `value`. A `retry` failure goes back to the model while the
    /// verification retries last, then is accepted with an `exhausted`
    /// event; a `stop` failure ends the run failed.
    pub(super) async fn verify(
        &mut self,
        text: &str,
        value: Option<JsonObject>,
    ) -> Result<Checked, RunError> {
        let Some(verifiers) = self.agent.verifiers.clone() else {
            return Ok(Checked::Valid(value));
        };
        let Some(failure) = verifiers.check(text) else {
            return Ok(Checked::Valid(value));
        };
        let (name, feedback) = (&failure.verifier, &failure.feedback);
        match failure.on_fail {
            OnFail::Stop => {
                self.stage_verification(name, VerificationAction::Stop, feedback);
                let message = format!("the final answer failed verifier {name:?}: {feedback}");
                let event =
                    json!({"name": "VerificationError", "message": message, "retryable": false});
                let error = event.as_object().cloned().unwrap_or_default();
                self.journal.stage(Event::Error(ErrorEvent { error }));
                let answer = Answer {
                    text: (!text.is_empty()).then(|| text.to_owned()),
                    structured_output: value,
                    structured_error: None,
                };
                let (status, subtype) = (RunStatus::Failed, ResultSubtype::Error);
                self.end_with(status, subtype, StopReason::Error, answer)
                    .await?;
                Ok(Checked::Ended)
            }
            OnFail::Retry if self.verify_retries < verifiers.max_retries => {
                self.verify_retries += 1;
                self.stage_verification(name, VerificationAction::Retry, feedback);
                Ok(Checked::Retry(self.send_back(feedback).await?))
            }
            OnFail::Retry => {
                self.stage_verification(name, VerificationAction::Exhausted, feedback);
                Ok(Checked::Valid(value))
            }
        }
    }

    /// Stage a `verification` event for `verifier`, with the retries its
    /// budget has spent.
    pub(super) fn stage_verification(
        &mut self,
        verifier: &str,
        action: VerificationAction,
        feedback: &str,
    ) {
        let attempt = match verifier {
            VERIFIER => self.schema_retries,
            _ => self.verify_retries,
        };
        self.journal.stage(Event::Verification(VerificationEvent {
            verifier: verifier.to_owned(),
            action,
            feedback: feedback.to_owned(),
            attempt: u64::from(attempt),
        }));
    }
}
