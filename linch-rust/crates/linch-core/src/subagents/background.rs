//! Background subagents: a child run the parent's stream drives while the
//! parent goes on, and the `<task-notification>` its end becomes.

use std::sync::{Arc, Weak};

use futures::StreamExt;

use super::run::{Outcome, failed, finished};
use super::workers::Worker;
use crate::agent::{AgentInner, RunOptions};
use crate::events::kinds::{ChildRunStatusEvent, ResultSubtype};
use crate::events::{Envelope, Event, Persistence, SubagentEvent};
use crate::ids::{RunId, SessionId};
use crate::json::{JsonObject, JsonValue};
use crate::policy::Principal;
use crate::run::RunStatus;
use crate::runner::{self, Family, RunStream};
use crate::scheduler::RunIdentity;
use crate::store::StoreError;
use crate::tool::{ToolContext, ToolOutput, ToolOutputError, ToolResult};

/// How a background child ended, for its notification.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Completion {
    pub(crate) child: RunId,
    pub(crate) display_name: String,
    /// `Completed`, `Failed` or `Aborted`.
    pub(crate) status: RunStatus,
    /// The final text, else the last response's text.
    pub(crate) text: String,
    /// The last error, as `name: message`.
    pub(crate) error: Option<String>,
}

impl Completion {
    fn of(child: RunId, display_name: String, outcome: Outcome) -> Self {
        let status = match outcome.ended {
            Some(ResultSubtype::Success) => RunStatus::Completed,
            Some(ResultSubtype::Aborted | ResultSubtype::Interrupted) => RunStatus::Aborted,
            Some(ResultSubtype::Error) | None => RunStatus::Failed,
        };
        Self {
            child,
            display_name,
            status,
            text: outcome.text,
            error: outcome.error,
        }
    }

    /// The `<task-notification>` the parent's model gets, every value
    /// escaped as Python's `html.escape` does.
    pub(crate) fn notification(&self) -> String {
        let status = match self.status {
            RunStatus::Completed => "completed",
            RunStatus::Aborted => "aborted",
            _ => "failed",
        };
        let error = match (&self.error, self.status) {
            (Some(error), RunStatus::Failed) => format!("<error>{}</error>", escape(error)),
            _ => String::new(),
        };
        format!(
            "<task-notification><task-id>{}</task-id><status>{status}</status>\
             <summary>Worker '{}' finished.</summary><result>{}</result>{error}\
             </task-notification>",
            escape(self.child.as_str()),
            escape(&self.display_name),
            escape(&self.text),
        )
    }
}

fn escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#x27;"),
            c => escaped.push(c),
        }
    }
    escaped
}

/// Start `worker` on `prompt` in the background of the call in `ctx`, or
/// pick up its child run again (a resumed parent runs the call again):
/// the parent's family drives it, and the call returns at once.
pub(super) async fn launch(
    parent: &AgentInner,
    worker: Worker,
    prompt: String,
    ctx: &ToolContext,
) -> ToolResult {
    let Some(family) = parent.family(&ctx.run_id) else {
        let message = "Background subagents need the parent's run stream to drive them.";
        return Err(ToolOutputError::new(message));
    };
    let id = worker.id.clone();
    let principal = ctx.principal.clone();
    pick_up(parent, &family, worker, prompt, principal, &ctx.run_id).await?;
    Ok(started(ctx, &id))
}

/// Have `family` drive the child run of `worker`: start it on `prompt`,
/// resume it, or put the end of one that finished in the inbox.
async fn pick_up(
    parent: &AgentInner,
    family: &Arc<Family>,
    worker: Worker,
    prompt: String,
    principal: Option<Principal>,
    parent_run: &RunId,
) -> Result<(), ToolOutputError> {
    let child = match parent.child(&worker.definition) {
        Ok(child) => Arc::new(child),
        Err(error) => return Err(failed("could not be built", error)),
    };
    let id = worker.id.clone();
    let cancel = family.child_token();
    let options = RunOptions {
        session_id: SessionId::new(id.as_str()),
        run_id: Some(id.clone()),
        principal,
        owner: "subagent".into(),
        cancel: cancel.clone(),
    };
    let stream = match child.runs.load(&child.namespace, &id).await {
        Err(StoreError::NotFound { .. }) => {
            runner::start_child(child, prompt, options, parent_run.clone())
        }
        Ok(record) if record.status.is_terminal() => {
            let outcome = finished(&child, &id).await?;
            family.complete(Completion::of(id, worker.display_name, outcome));
            return Ok(());
        }
        Ok(_) => runner::resume(child, id.clone(), None, options),
        Err(error) => return Err(failed("could not be read", error)),
    };
    let drive = drive(stream, worker, Arc::downgrade(family));
    family.adopt(id, cancel, true, drive);
    Ok(())
}

/// Pick up the background children of a resumed run: each child that a
/// `child_run_status` event in `events` marks running, with no later mark
/// of its end, is resumed, started (its create never landed), or read
/// back. A child that cannot be picked up reports a failed end.
pub(crate) async fn reattach(
    parent: &AgentInner,
    family: &Arc<Family>,
    run: &RunIdentity,
    events: &[Envelope],
) {
    let Some(roster) = &parent.subagents else {
        return;
    };
    let mut running: Vec<&RunId> = Vec::new();
    for envelope in events {
        if let Event::ChildRunStatus(mark) = &envelope.event {
            running.retain(|id| **id != mark.child_run_id);
            if mark.status == RunStatus::Running {
                running.push(&mark.child_run_id);
            }
        }
    }
    let prefix = format!("{}:", run.run_id);
    for id in running {
        let Some(input) = call_input(events, id.as_str().strip_prefix(&prefix)) else {
            continue;
        };
        let text = |key: &str| input.get(key).and_then(JsonValue::as_str);
        let Some(definition) = roster.get(text("subagent_type")) else {
            continue;
        };
        let general = std::ptr::eq(definition, roster.general());
        let registry = &parent.worker_registry;
        let worker = registry.enlist(&run.session_id, id, definition, general);
        let name = worker.display_name.clone();
        let prompt = text("prompt").unwrap_or_default().to_owned();
        let principal = run.principal.clone();
        let picked = pick_up(parent, family, worker, prompt, principal, &run.run_id).await;
        if let Err(error) = picked {
            family.complete(Completion {
                child: id.clone(),
                display_name: name,
                status: RunStatus::Failed,
                text: String::new(),
                error: Some(error.message),
            });
        }
    }
}

/// The input of the call `tool_use_id` among `events`.
fn call_input<'a>(events: &'a [Envelope], tool_use_id: Option<&str>) -> Option<&'a JsonObject> {
    let tool_use_id = tool_use_id?;
    events.iter().find_map(|envelope| match &envelope.event {
        Event::ToolCallStart(start) if start.tool_use_id == tool_use_id => Some(&start.input),
        _ => None,
    })
}

/// The acknowledgement, after marking the child as running in the
/// parent's log.
fn started(ctx: &ToolContext, id: &RunId) -> ToolOutput {
    ctx.emit(Event::ChildRunStatus(ChildRunStatusEvent {
        child_run_id: id.clone(),
        status: RunStatus::Running,
    }));
    ToolOutput::new(format!(
        "Worker '{id}' started in background. You will receive a <task-notification> when it \
         finishes."
    ))
}

/// Drive the child's stream to its end: its durable events go into the
/// parent's log, wrapped, at the parent's next turn; its end becomes a
/// completion in the family's inbox.
async fn drive(mut stream: RunStream, worker: Worker, family: Weak<Family>) {
    let mut outcome = Outcome::default();
    while let Some(item) = stream.next().await {
        let envelope = match item {
            Ok(envelope) => envelope,
            Err(error) => {
                outcome.error = Some(format!("RunError: {error}"));
                break;
            }
        };
        outcome.see(&envelope.event);
        if envelope.event.persistence() != Persistence::Both {
            continue;
        }
        if let Some(family) = family.upgrade() {
            family.report(Event::Subagent(SubagentEvent {
                child_run_id: worker.id.clone(),
                event: Box::new(envelope.event),
            }));
        }
    }
    if let Some(family) = family.upgrade() {
        family.complete(Completion::of(worker.id, worker.display_name, outcome));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_notification_escapes_its_values_and_names_a_failure() {
        let mut completion = Completion {
            child: RunId::new("r:s"),
            display_name: "it's".into(),
            status: RunStatus::Completed,
            text: "a < \"b\"".into(),
            error: None,
        };
        assert_eq!(
            completion.notification(),
            "<task-notification><task-id>r:s</task-id><status>completed</status>\
             <summary>Worker 'it&#x27;s' finished.</summary>\
             <result>a &lt; &quot;b&quot;</result></task-notification>"
        );
        completion.status = RunStatus::Failed;
        completion.error = Some("TurnLimitError: max turns & more".into());
        assert_eq!(
            completion.notification(),
            "<task-notification><task-id>r:s</task-id><status>failed</status>\
             <summary>Worker 'it&#x27;s' finished.</summary>\
             <result>a &lt; &quot;b&quot;</result>\
             <error>TurnLimitError: max turns &amp; more</error></task-notification>"
        );
    }
}
