//! Driving one subagent's child run and turning its end into a result.

use std::sync::Arc;

use futures::StreamExt;
use tokio::sync::oneshot;

use super::SubagentDefinition;
use crate::agent::{AgentInner, RunOptions};
use crate::events::kinds::ResultSubtype;
use crate::events::{Event, SubagentEvent};
use crate::ids::{RunId, SessionId};
use crate::json::{JsonObject, JsonValue};
use crate::runner::{self, RunStream};
use crate::store::StoreError;
use crate::tool::{ToolContext, ToolOutput, ToolOutputError, ToolResult};

/// Run the `definition` subagent on `prompt` as the child run of the call
/// in `ctx`: start it, resume it if a crash left it unfinished, or read
/// the result of one that finished. Each event of the child goes into the
/// parent's log wrapped in a `subagent` event.
///
/// The parent run's [`Family`](crate::runner::Family) drives the child, so
/// a call cut short (by its timeout or the run's cancel, which cancel the
/// call's token and so the child's) leaves the child to write its abort.
pub(super) async fn run(
    parent: &AgentInner,
    definition: &SubagentDefinition,
    prompt: String,
    ctx: &ToolContext,
) -> ToolResult {
    let child = match parent.child(definition) {
        Ok(child) => Arc::new(child),
        Err(error) => return Err(failed("could not be built", error)),
    };
    let child_id = RunId::new(ctx.idempotency_key());
    let cancel = ctx.cancel.child_token();
    let options = RunOptions {
        session_id: SessionId::new(child_id.as_str()),
        run_id: Some(child_id.clone()),
        principal: ctx.principal.clone(),
        owner: "subagent".into(),
        cancel: cancel.clone(),
    };
    let stream = match child.runs.load(&child.namespace, &child_id).await {
        Err(StoreError::NotFound { .. }) => {
            runner::start_child(child, prompt, options, ctx.run_id.clone())
        }
        Ok(record) if record.status.is_terminal() => {
            let events = (child.runs.events(&child.namespace, &child_id, 0)).await;
            let events = events.map_err(|error| failed("could not be read", error))?;
            let mut outcome = Outcome::default();
            events
                .iter()
                .for_each(|envelope| outcome.see(&envelope.event));
            return outcome.result();
        }
        Ok(_) => runner::resume(child, child_id.clone(), None, options),
        Err(error) => return Err(failed("could not be read", error)),
    };
    let Some(family) = parent.family(&ctx.run_id) else {
        return drive(stream, child_id, ctx).await;
    };
    let (done, result) = oneshot::channel();
    let (id, ctx) = (child_id.clone(), ctx.clone());
    family.adopt(child_id, cancel, async move {
        let _ = done.send(drive(stream, id, &ctx).await);
    });
    let result = result.await;
    result.unwrap_or_else(|_| Err(failed("stopped", "its parent run went away")))
}

async fn drive(mut stream: RunStream, child_run_id: RunId, ctx: &ToolContext) -> ToolResult {
    let mut outcome = Outcome::default();
    while let Some(item) = stream.next().await {
        let envelope = item.map_err(|error| failed("could not continue", error))?;
        outcome.see(&envelope.event);
        ctx.emit(Event::Subagent(SubagentEvent {
            child_run_id: child_run_id.clone(),
            event: Box::new(envelope.event),
        }));
    }
    outcome.result()
}

fn failed(what: &str, error: impl std::fmt::Display) -> ToolOutputError {
    ToolOutputError::new(format!("The subagent run {what}: {error}"))
}

/// What the child's events say about how it ended.
#[derive(Default)]
struct Outcome {
    /// The final text, else the last response's text.
    text: String,
    /// The last error, as `name: message`.
    error: Option<String>,
    ended: Option<ResultSubtype>,
}

impl Outcome {
    fn see(&mut self, event: &Event) {
        match event {
            Event::Assistant(response) => self.text = response.message.text(),
            Event::Error(error) => self.error = Some(error_text(&error.error)),
            Event::Result(result) => {
                if let Some(text) = &result.final_text {
                    self.text = text.clone();
                }
                self.ended = Some(result.subtype);
            }
            _ => {}
        }
    }

    /// The tool result: the text of a successful child, else an error that
    /// keeps any partial output.
    fn result(self) -> ToolResult {
        let partial = |message: String| {
            let message = match self.text.is_empty() {
                true => message,
                false => format!("{message} Partial output: {}", self.text),
            };
            Err(ToolOutputError::new(message))
        };
        match self.ended {
            Some(ResultSubtype::Success) if !self.text.is_empty() => Ok(ToolOutput::new(self.text)),
            Some(ResultSubtype::Success) => partial("Subagent produced no text output.".into()),
            Some(ResultSubtype::Aborted | ResultSubtype::Interrupted) => {
                partial("Subagent aborted.".into())
            }
            Some(ResultSubtype::Error) => {
                let error = self.error.as_ref().map(|e| format!(": {e}"));
                partial(format!(
                    "Subagent encountered an error{}.",
                    error.unwrap_or_default()
                ))
            }
            None => partial("Subagent stopped without a result.".into()),
        }
    }
}

fn error_text(error: &JsonObject) -> String {
    let field = |key: &str| error.get(key).and_then(JsonValue::as_str).unwrap_or("");
    let name = match field("name") {
        "" => "Error",
        name => name,
    };
    format!("{name}: {}", field("message"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::kinds::{AssistantEvent, ErrorEvent, ResultEvent};
    use crate::message::Message;
    use crate::run::StopReason;
    use crate::usage::Usage;

    fn ended(subtype: ResultSubtype, final_text: Option<&str>) -> Event {
        Event::Result(ResultEvent {
            subtype,
            stop_reason: StopReason::EndTurn,
            total_usage: Usage::default(),
            usage_complete: true,
            duration_ms: 0,
            total_cost_usd: None,
            final_text: final_text.map(str::to_owned),
            structured_output: None,
            structured_error: None,
            trace_id: None,
        })
    }

    fn outcome(events: &[Event]) -> ToolResult {
        let mut outcome = Outcome::default();
        events.iter().for_each(|event| outcome.see(event));
        outcome.result()
    }

    fn message(result: ToolResult) -> String {
        result.unwrap_err().message
    }

    #[test]
    fn the_child_end_becomes_the_tool_result() {
        let said = Event::Assistant(AssistantEvent {
            message: Message::assistant_text("half"),
            stop_reason: StopReason::ToolUse,
        });
        let success = outcome(&[ended(ResultSubtype::Success, Some("all"))]).unwrap();
        assert_eq!(success.value, "all");
        let silent = outcome(&[ended(ResultSubtype::Success, None)]);
        assert_eq!(message(silent), "Subagent produced no text output.");
        let aborted = outcome(&[said.clone(), ended(ResultSubtype::Aborted, None)]);
        assert_eq!(message(aborted), "Subagent aborted. Partial output: half");
        let error = serde_json::json!({"name": "TurnLimitError", "message": "max turns exceeded"});
        let error = Event::Error(ErrorEvent {
            error: error.as_object().unwrap().clone(),
        });
        let failed = outcome(&[error, ended(ResultSubtype::Error, None)]);
        assert_eq!(
            message(failed),
            "Subagent encountered an error: TurnLimitError: max turns exceeded."
        );
        assert_eq!(
            message(outcome(&[said])),
            "Subagent stopped without a result. Partial output: half"
        );
    }
}
