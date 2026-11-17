//! The run's final tool (see [`crate::structured::FinalTool`]): a response
//! that calls it ends the run with the call's input as the answer.
//!
//! The call never runs, so no `tool_call_start` or `tool_call_end` is
//! written for it, but it is decided as any call of a batch is
//! ([`crate::scheduler::plan_one`]): the tool's input schema and
//! [`crate::Tool::validate`], the `pre_tool_use` chain (which may rewrite
//! the input, and whose result is the input the answer is made of), and the
//! permission rules. A hook's veto, a deny rule and an invalid input go
//! back to the model as the call's error result; an `Ask` is taken as
//! allowed, since nothing runs and a call that never runs cannot wait for
//! an approval. They draw on the schema gate's `structured_output_retries`,
//! so a veto that never lifts ends the run failed instead of looping.
//!
//! Before any of that, a background child or a peer message that is
//! waiting is heard as a text answer hears it ([`Runner::hear_back_final`]):
//! the result of the final call says the answer was not taken, and the
//! notes ride in the same message. Then the input is checked against the
//! run's output schema as a text answer is, with the same
//! `structured_output_retries` and `verification` events: a failure goes
//! back to the model as the call's error result, and past the retries ends
//! the run failed. A call beside other tool calls is such a failure, and
//! none of the calls runs. An input that passes meets the verifiers and the
//! `stop` hooks, which see it as JSON text, the result's `final_text`; a
//! rewrite by one is parsed and checked again, so `structured_output` is
//! always the object `final_text` holds.
//!
//! Every way out leaves the response's calls paired in the session. The
//! feedback for the model is their results. A terminal first holds them,
//! with the answer, in a `Finishing` checkpoint that the session append
//! makes durable, then appends them, then ends the run; a resume from
//! `Finishing` only appends (unless the session has them) and ends.

use std::sync::Arc;

use super::end::Answer;
use super::hooks::{NOT_RUN_TEXT, Stopping};
use super::structured::Checked;
use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ResultSubtype, UserEvent, UserEventSubtype};
use crate::json::{JsonObject, JsonValue};
use crate::message::{ContentBlock, Message, Role, ToolResultBlock, ToolUseBlock};
use crate::registry::RegisteredTool;
use crate::run::{Ending, Phase, RunStatus, StopReason};
use crate::scheduler::{self, PlannedAction};
use crate::structured::{FINAL_ACK, FinalTool, feedback};

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
        self.results_with(text, !taken, Vec::new())
    }

    /// The results as [`Self::results`] gives them for a call that was not
    /// taken, with `notes` after them in the same message.
    pub(super) fn heard(&self, text: &str, notes: Vec<ContentBlock>) -> Message {
        self.results_with(text, true, notes)
    }

    fn results_with(&self, text: &str, is_error: bool, notes: Vec<ContentBlock>) -> Message {
        let results = self.response.tool_uses().map(|call| {
            let block = match call.id == self.call {
                true => ToolResultBlock::text(&call.id, text, is_error),
                false => ToolResultBlock::text(&call.id, NOT_RUN_TEXT, true),
            };
            ContentBlock::ToolResult(block)
        });
        Message::new(Role::User, results.chain(notes).collect())
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
        let block = call.clone();
        self.unpaired = Some(Unanswered {
            response,
            call: block.id.clone(),
        });
        // Background children and peers answer before the run ends, as for
        // a text answer; the call is paired with their notes.
        if let Some(next) = self.hear_back_final().await? {
            return Ok(next);
        }
        if let Err(error) = tool.alone(calls) {
            let text = JsonValue::Object(block.input.clone()).to_string();
            return Ok(step(self.schema_retry(&text, error).await?));
        }
        let (input, registered) = match self.decide(&tool, &block).await? {
            Decided::Go { input, tool } => (input, tool),
            Decided::Over(next) => return Ok(next),
        };
        let text = JsonValue::Object(input.clone()).to_string();
        let decoded = match &self.gates {
            Some(_) => crate::gates::leaves(&input),
            None => Vec::new(),
        };
        if let Some(checked) = self.gate_answer(&text, &decoded).await? {
            return Ok(step(checked));
        }
        let checked = match &self.output {
            Some(output) => output.check_value(JsonValue::Object(input)).map(Some),
            None => Ok(Some(input)),
        };
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
        if self.gate_rewrite(&text, &answer).await?.is_some() {
            return Ok(None);
        }
        let value = match self.recheck(&registered, &text, &answer, value).await? {
            Checked::Valid(value) => value,
            other => return Ok(step(other)),
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

    /// Decide the final call as a batch's call is decided: its input is
    /// checked with the tool, the `pre_tool_use` chain may rewrite or veto
    /// it, and permission may deny it. A refusal goes back to the model
    /// through the schema gate's retries.
    async fn decide(
        &mut self,
        final_tool: &FinalTool,
        block: &ToolUseBlock,
    ) -> Result<Decided, RunError> {
        let model_text = JsonValue::Object(block.input.clone()).to_string();
        let snapshot = self.agent.snapshot();
        let registered = match final_tool.registered(&snapshot) {
            Ok(registered) => registered,
            Err(error) => {
                let checked = self.schema_retry(&model_text, error).await?;
                return Ok(Decided::Over(step(checked)));
            }
        };
        let (agent, run) = (&self.agent, &self.run);
        let planned =
            scheduler::plan_one(block, &registered, &agent.permissions, &agent.hooks, run);
        let Some(planned) = self.cancel.run_until_cancelled(planned).await else {
            self.abort().await?;
            return Ok(Decided::Over(None));
        };
        self.push_records(planned.hook_records.clone(), Some(&block.id));
        let name = &final_tool.name;
        let refusal = match planned.action {
            PlannedAction::Error { error } => {
                let feedback = feedback(&error.message, Some(name));
                let text = JsonValue::Object(planned.input).to_string();
                let why = "the final tool call does not satisfy the tool's input schema";
                let checked = self
                    .retry_or_fail(&text, error.message, feedback, why.to_owned())
                    .await?;
                return Ok(Decided::Over(step(checked)));
            }
            PlannedAction::Blocked { reason } => {
                format!("a pre_tool_use hook blocked it: {reason}")
            }
            PlannedAction::Denied { reason } => format!("permission denied it: {reason}"),
            // Nothing runs, so there is nothing to serve, ask for or wait on.
            PlannedAction::Run
            | PlannedAction::Serve { .. }
            | PlannedAction::AwaitingApproval { .. }
            | PlannedAction::AwaitingInput => {
                let (input, tool) = (planned.input, registered);
                return Ok(Decided::Go { input, tool });
            }
        };
        let text = JsonValue::Object(planned.input).to_string();
        let feedback = format!(
            "Your call of the {name} tool was refused: {refusal}\nCall it again with a \
             different input, or answer without it."
        );
        let why = "the final tool call was refused, and the retries are spent";
        let checked = self
            .retry_or_fail(&text, refusal, feedback, why.to_owned())
            .await?;
        Ok(Decided::Over(step(checked)))
    }

    /// Check `answer`, what the `stop` chain left of `text`, once more when
    /// a hook rewrote it: it must be an object that the output schema (if
    /// any) and the final tool accept, and it becomes the `structured_output`
    /// the result holds next to it. The model did not write it, so a
    /// failure ends the run without going back to it.
    async fn recheck(
        &mut self,
        tool: &RegisteredTool,
        text: &str,
        answer: &str,
        value: Option<JsonObject>,
    ) -> Result<Checked, RunError> {
        if answer == text {
            return Ok(Checked::Valid(value));
        }
        let parsed = match &self.output {
            Some(output) => output.check(answer),
            None => match serde_json::from_str::<JsonValue>(answer.trim()) {
                Ok(JsonValue::Object(object)) => Ok(object),
                Ok(_) => Err("Expected a JSON object".to_owned()),
                Err(error) => Err(format!("JSON parse error: {error}")),
            },
        };
        let checked = parsed.and_then(|object| {
            let accepted = tool.validate_input(object);
            accepted.map_err(|error| error.message)
        });
        match checked {
            Ok(object) => Ok(Checked::Valid(Some(object))),
            Err(error) => {
                let why = "a stop hook rewrote the final answer into one the final tool does not \
                           accept";
                self.schema_failed(answer, why.to_owned(), error).await?;
                Ok(Checked::Ended)
            }
        }
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

/// What deciding the final call came to.
enum Decided {
    /// Go on with this input, the tool's own and after any hook rewrite.
    Go {
        input: JsonObject,
        tool: Arc<RegisteredTool>,
    },
    /// The call went back to the model or the run ended: this is next.
    Over(Option<Step>),
}

/// The step after a check that did not take the answer.
fn step(checked: Checked) -> Option<Step> {
    match checked {
        Checked::Retry(step) => Some(step),
        Checked::Valid(_) | Checked::Ended => None,
    }
}
