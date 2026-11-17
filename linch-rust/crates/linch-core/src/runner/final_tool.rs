//! The run's final tool (see [`crate::structured::FinalTool`]): a response
//! that calls it ends the run with the call's input as the answer.
//!
//! The call never runs, so no `tool_call_start` or `tool_call_end` is
//! written for it. Its input is checked against the run's output schema as
//! a text answer is, with the same `structured_output_retries` and
//! `verification` events: a failure goes back to the model as the call's
//! error result, and past the retries ends the run failed. A call beside
//! other tool calls is such a failure, and none of the calls runs. An input
//! that passes meets the verifiers and the `stop` hooks, which see it as
//! JSON text, the result's `final_text`.
//!
//! Every way out leaves the response's calls paired in the session. The
//! feedback for the model is their results. A terminal first holds them,
//! with the answer, in a `Finishing` checkpoint that the session append
//! makes durable, then appends them, then ends the run; a resume from
//! `Finishing` only appends (unless the session has them) and ends.

use super::end::Answer;
use super::hooks::{NOT_RUN_TEXT, Stopping};
use super::structured::Checked;
use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ResultSubtype, UserEvent, UserEventSubtype};
use crate::json::JsonValue;
use crate::message::{ContentBlock, Message, Role, ToolResultBlock};
use crate::run::{Ending, Phase, RunStatus, StopReason};
use crate::structured::FINAL_ACK;

/// The result of a final tool call the run ended on without taking it.
const NOT_TAKEN: &str = "Final answer not taken: the run ended first.";

/// A logged response that called the final tool, none of its calls
/// answered yet.
pub(super) struct Unanswered {
    response: Message,
    /// The id of its final tool call.
    call: String,
}

impl Unanswered {
    /// The results that pair the response: `text` for the final tool call,
    /// an error unless `taken`, and not run for every other call.
    pub(super) fn results(&self, text: &str, taken: bool) -> Message {
        let results = self.response.tool_uses().map(|call| {
            let block = match call.id == self.call {
                true => ToolResultBlock::text(&call.id, text, !taken),
                false => ToolResultBlock::text(&call.id, NOT_RUN_TEXT, true),
            };
            ContentBlock::ToolResult(block)
        });
        Message::new(Role::User, results.collect())
    }

    /// The results for a run that ends as `ending` without taking the
    /// call: the schema error when there is one.
    pub(super) fn ended(&self, ending: &Ending) -> Message {
        let text = ending.structured_error.as_deref().unwrap_or(NOT_TAKEN);
        self.results(text, false)
    }
}

impl Runner {
    /// Decide on `response`, a logged response that calls the final tool
    /// and stopped for `stop_reason`: take the call's input as the answer
    /// and end the run, or send it back.
    pub(super) async fn final_call(
        &mut self,
        response: Message,
        stop_reason: StopReason,
    ) -> Result<Option<Step>, RunError> {
        let Some(tool) = self.final_tool.clone() else {
            unreachable!("only a run with a final tool calls it");
        };
        let calls = response.tool_uses().count();
        let Some(call) = response.tool_uses().find(|call| call.name == tool.name) else {
            unreachable!("the response calls the final tool");
        };
        let (id, input) = (call.id.clone(), call.input.clone());
        self.unpaired = Some(Unanswered { response, call: id });
        let text = JsonValue::Object(input.clone()).to_string();
        let checked = tool.alone(calls).and_then(|()| match &self.output {
            Some(output) => output.check_value(JsonValue::Object(input)).map(Some),
            None => Ok(Some(input)),
        });
        let value = match checked {
            Ok(value) => value,
            Err(error) => return Ok(step(self.schema_retry(&text, error).await?)),
        };
        let value = match self.verify(&text, value).await? {
            Checked::Valid(value) => value,
            other => return Ok(step(other)),
        };
        let answer = match self.stop(&text, value.as_ref()).await? {
            Stopping::Stop { text } => text,
            Stopping::Continue(feedback) => return Ok(Some(feedback)),
            Stopping::Ended => return Ok(None),
        };
        // Without a schema, a rewrite changes the text, not the value.
        let value = match self.output.is_some() {
            true => match self.check_rewrite(&text, &answer, value).await? {
                Checked::Valid(value) => value,
                other => return Ok(step(other)),
            },
            false => value,
        };
        if !self.post_turn(stop_reason).await {
            self.abort().await?;
            return Ok(None);
        }
        let Some(unanswered) = self.unpaired.take() else {
            unreachable!("nothing paired the call before the answer was taken");
        };
        let ending = Ending {
            status: RunStatus::Completed,
            stop_reason,
            final_text: Some(answer),
            structured_output: value,
            structured_error: None,
        };
        self.finish(unanswered.results(FINAL_ACK, true), ending)
            .await?;
        Ok(None)
    }

    /// Hold `message`, the results that pair a final tool call, and
    /// `ending` in a `Finishing` checkpoint, then append them and end the
    /// run.
    pub(super) async fn finish(
        &mut self,
        message: Message,
        ending: Ending,
    ) -> Result<(), RunError> {
        self.journal.stage(Event::User(UserEvent {
            message: message.clone(),
            subtype: UserEventSubtype::ToolResult,
        }));
        let phase = Phase::Finishing {
            message: message.clone(),
            ending: ending.clone(),
        };
        self.save(phase).await?;
        self.finishing(message, ending, false).await
    }

    /// Append `message` unless `appended`, then end the run as `ending`
    /// says.
    pub(super) async fn finishing(
        &mut self,
        message: Message,
        ending: Ending,
        appended: bool,
    ) -> Result<(), RunError> {
        if !appended {
            self.append(message).await?;
        }
        let subtype = match ending.status {
            RunStatus::Completed => ResultSubtype::Success,
            RunStatus::Aborted => ResultSubtype::Aborted,
            _ => ResultSubtype::Error,
        };
        let answer = Answer {
            text: ending.final_text,
            structured_output: ending.structured_output,
            structured_error: ending.structured_error,
        };
        (self.terminal(ending.status, subtype, ending.stop_reason, answer)).await
    }

    /// Send `feedback` back to the model as the user message that ends
    /// this turn: the results of a final tool call being decided on, else
    /// a system reminder.
    pub(super) async fn send_back(&mut self, feedback: &str) -> Result<Step, RunError> {
        let message = match self.unpaired.take() {
            Some(unanswered) => unanswered.results(feedback, false),
            None => Message::user_text(crate::skills::system_reminder(feedback)),
        };
        self.feedback(message).await
    }
}

/// The step after a check that did not take the answer.
fn step(checked: Checked) -> Option<Step> {
    match checked {
        Checked::Retry(step) => Some(step),
        Checked::Valid(_) | Checked::Ended => None,
    }
}
