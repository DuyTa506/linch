//! Journaled agent calls: an agent's run on a prompt, as a workflow call
//! whose final text is journaled and whose run a resume continues.

use futures::StreamExt;
use linch_core::events::kinds::{ResultSubtype, WorkflowKind};
use linch_core::{
    Agent, Event, JsonObject, JsonValue, RunError, RunId, RunOptions, RunStream, SessionId,
    StoreError, digest_with_domain,
};
use serde_json::json;

use super::context::{WorkflowContext, decode, event};
use super::error::WorkflowError;
use super::journal::agent_key;

impl WorkflowContext {
    /// Run `agent` on `prompt` as a journaled call named `name` and return
    /// its final text.
    ///
    /// Each call is its own run of `agent`, on a new session, with a run id
    /// derived from the workflow, the call's key ([`agent_key`] of `name`
    /// and `prompt`, plus its branch path inside a fan-out) and its
    /// occurrence. So when a process crashes during the call, the next
    /// [`run_workflow`](super::run_workflow) continues that run instead of
    /// starting over, and never pays a model call twice (the run's own
    /// journal sees to that). The final text is journaled
    /// once the run succeeds; a resume after that replays it without
    /// touching the agent.
    ///
    /// Once the run has ended, the call closes its session
    /// ([`Agent::close_session`]), which frees what the agent holds for it
    /// in this process: after a success once the text is journaled, and
    /// after a failed or aborted run. A run waiting for an approval keeps
    /// its session, since the approve continues on it.
    ///
    /// A run that failed or was aborted is never continued: the call fails,
    /// and the next resume runs the call again as a fresh run. A run that
    /// stops for an approval also fails the call; approve it with
    /// [`Agent::approve`] (drain the stream), then resume the workflow,
    /// which reads the finished run's text. A run the agent can no longer
    /// continue (say, a [`RunError::ContractMismatch`] after the agent
    /// changed) fails the call on every resume until the host ends it with
    /// [`Agent::fail`]. The workflow's cancel token
    /// aborts the run; step timeouts and retries do not apply, the
    /// workflow deadline does.
    ///
    /// Args:
    ///     agent: The agent to run; its namespace and stores hold the run.
    ///     name: The call's name, in its key and on its workflow events
    ///         (as `title` and `subagent_type`).
    ///     prompt: The prompt of the run.
    ///
    /// Returns:
    ///     The run's final text, else the text of its last response.
    ///
    /// Raises:
    ///     WorkflowError: `Aborted` when the cancel token fired before the
    ///         call or the run was aborted; `Step` when the run failed, is
    ///         waiting for an approval, or could not be read or continued;
    ///         `Store` when the journal write fails.
    pub async fn agent(
        &self,
        agent: &Agent,
        name: &str,
        prompt: &str,
    ) -> Result<String, WorkflowError> {
        let label = format!("agent {name}");
        let (call_key, occurrence, cached) = self.next_occurrence(&agent_key(name, prompt));
        let emit = |kind, result_text: Option<&str>| {
            let mut event = event(kind, name.to_owned(), call_key.clone(), occurrence);
            event.subagent_type = name.to_owned();
            event.result_text = result_text.map(str::to_owned);
            self.emit(event);
        };
        if let Some(value) = cached {
            let text: String = decode(&label, value)?;
            emit(WorkflowKind::AgentReplayed, Some(&text));
            return Ok(text);
        }
        self.check_cancel(&label)?;
        emit(WorkflowKind::AgentStart, None);
        let lease = &self.inner.lease;
        let mut generation = 0;
        let (run_id, outcome) = loop {
            let run_id = agent_run_id(
                &lease.namespace,
                &lease.workflow_id,
                &call_key,
                occurrence,
                generation,
            );
            let options = RunOptions {
                session_id: SessionId::new(run_id.as_str()),
                run_id: Some(run_id.clone()),
                principal: None,
                owner: "workflow".into(),
                cancel: self.inner.cancel.child_token(),
                trace_id: None,
            };
            let stream = match agent.events(&run_id, 0).await {
                Err(RunError::Store(StoreError::NotFound { .. })) => agent.run(prompt, options),
                Ok(events) => {
                    let outcome = Outcome::of(events.iter().map(|envelope| &envelope.event));
                    match outcome.ended {
                        Some(ResultSubtype::Success) => break (run_id, Ok(outcome)),
                        // An earlier attempt failed: this one is a fresh run.
                        Some(_) => {
                            generation += 1;
                            continue;
                        }
                        None => agent.resume(run_id.clone(), options),
                    }
                }
                Err(error) => break (run_id, Err(error)),
            };
            break (run_id, drive(stream).await);
        };
        // A run that ended never continues on its session, while a run
        // waiting for an approval does.
        let session = SessionId::new(run_id.as_str());
        let ended = outcome
            .as_ref()
            .is_ok_and(|outcome| outcome.ended.is_some());
        let text = finish(name, &label, &run_id, outcome).inspect_err(|_| {
            if ended {
                agent.close_session(&session);
            }
        })?;
        self.record(&call_key, occurrence, &JsonValue::String(text.clone()))
            .await?;
        agent.close_session(&session);
        emit(WorkflowKind::AgentEnd, Some(&text));
        Ok(text)
    }
}

/// The id of the run that attempt `generation` of an `agent` call makes:
/// the same on every resume, and distinct per workflow and call.
pub(super) fn agent_run_id(
    namespace: &str,
    workflow_id: &str,
    call_key: &str,
    occurrence: u64,
    generation: u64,
) -> RunId {
    let value = json!({
        "namespace": namespace,
        "workflow_id": workflow_id,
        "call_key": call_key,
        "occurrence": occurrence,
        "generation": generation,
    });
    let digest = digest_with_domain("workflow.agent", &value);
    RunId::new(format!(
        "wf-{}",
        digest.expect("a flat JSON object always encodes")
    ))
}

async fn drive(mut stream: RunStream) -> Result<Outcome, RunError> {
    let mut outcome = Outcome::default();
    while let Some(item) = stream.next().await {
        outcome.see(&item?.event);
    }
    Ok(outcome)
}

/// The call's text, or why it has none.
fn finish(
    name: &str,
    label: &str,
    run_id: &RunId,
    outcome: Result<Outcome, RunError>,
) -> Result<String, WorkflowError> {
    let failed = |message: String| WorkflowError::Step {
        name: name.to_owned(),
        source: message.into(),
    };
    let outcome = outcome.map_err(|error| failed(format!("agent run {run_id}: {error}")))?;
    match outcome.ended {
        Some(ResultSubtype::Success) => Ok(outcome.text),
        Some(ResultSubtype::Aborted | ResultSubtype::Interrupted) => Err(WorkflowError::Aborted {
            label: label.to_owned(),
        }),
        Some(ResultSubtype::Error) => {
            let error = outcome.error.unwrap_or_else(|| "no error event".into());
            Err(failed(format!("agent run {run_id} failed: {error}")))
        }
        None if outcome.suspended => Err(failed(format!(
            "agent run {run_id} is waiting for approval; approve it, then resume the workflow"
        ))),
        None => Err(failed(format!("agent run {run_id} ended without a result"))),
    }
}

/// What a run's events say about how it ended.
#[derive(Default)]
struct Outcome {
    /// The final text, else the last response's text.
    text: String,
    /// The last error event's message.
    error: Option<String>,
    ended: Option<ResultSubtype>,
    suspended: bool,
}

impl Outcome {
    fn of<'a>(events: impl Iterator<Item = &'a Event>) -> Self {
        let mut outcome = Self::default();
        events.for_each(|event| outcome.see(event));
        outcome
    }

    fn see(&mut self, event: &Event) {
        match event {
            Event::Assistant(response) => self.text = response.message.text(),
            Event::Error(error) => self.error = Some(message(&error.error)),
            Event::Suspended(_) => self.suspended = true,
            Event::Result(result) => {
                if let Some(text) = &result.final_text {
                    self.text = text.clone();
                }
                self.ended = Some(result.subtype);
            }
            _ => {}
        }
    }
}

/// An error payload's `message`, or the payload as JSON when it has none.
fn message(error: &JsonObject) -> String {
    match error.get("message") {
        Some(JsonValue::String(message)) => message.clone(),
        _ => JsonValue::Object(error.clone()).to_string(),
    }
}
