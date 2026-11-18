//! Journaled agent calls: an agent's run on a prompt, as a workflow call
//! whose final text is journaled and whose run a resume continues.

use std::pin::pin;
use std::time::Duration;

use futures::StreamExt;
use futures::future::{Either, select};
use linch_core::events::kinds::{ResultSubtype, WorkflowKind};
use linch_core::{
    Agent, Event, ForkError, JsonObject, JsonValue, OutputSchema, RunError, RunId, RunOptions,
    RunStream, SessionId, StoreError, digest_with_domain,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::context::{WorkflowContext, decode, encode_error, event, lock};
use super::error::WorkflowError;
use super::journal::agent_key_with;
use super::policy::Retry;

/// One journaled agent call, for [`WorkflowContext::agent_with`].
#[derive(Debug, Clone, Default)]
pub struct AgentCall {
    /// The call's name, in its key and on its events (as `subagent_type`).
    pub name: String,
    /// The prompt of the run.
    pub prompt: String,
    /// The `title` of the call's events when it is not the name; it does
    /// not affect the key.
    pub label: Option<String>,
    /// A caller-owned identity in place of the prompt and options: the
    /// call replays while this stays the same, whatever else changes.
    pub key: Option<String>,
    /// What the caller built the agent with (its tool filter, say), hashed
    /// into the key so that a change makes this call miss its record. Unset
    /// leaves the key as [`agent_key`](super::agent_key) makes it.
    pub identity: Option<JsonValue>,
    /// The run's own output schema, in place of the agent's. The journaled
    /// text is then the compact JSON of the checked answer. It is hashed
    /// into the key.
    pub output_schema: Option<OutputSchema>,
    /// A session to continue from: the run starts on a copy of its durable
    /// log ([`Agent::fork_session`]). It is hashed into the key. Copy it
    /// at a fixed point: a live run on it may append meanwhile, and a
    /// call that starts later copies more.
    pub fork: Option<SessionId>,
    /// Limit on each attempt, which aborts the run; `None` or zero is
    /// none.
    pub timeout: Option<Duration>,
    /// Retries after a failed or timed out attempt; each is a fresh run.
    pub retry: Option<Retry>,
    /// Aborts the call's run. Pass a child of the workflow's cancel token
    /// to stop one call without stopping the workflow; `None` uses the
    /// workflow's own.
    pub cancel: Option<CancellationToken>,
}

impl AgentCall {
    /// A call named `name` that runs `prompt`, with no options.
    pub fn new(name: impl Into<String>, prompt: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            prompt: prompt.into(),
            ..Self::default()
        }
    }

    /// The options hashed into the call's key; `None` when it sets none.
    fn options(&self) -> Result<Option<JsonValue>, serde_json::Error> {
        let mut options = JsonObject::new();
        if let Some(identity) = &self.identity {
            options.insert("identity".into(), identity.clone());
        }
        if let Some(schema) = &self.output_schema {
            options.insert("output_schema".into(), serde_json::to_value(schema)?);
        }
        if let Some(source) = &self.fork {
            options.insert("fork".into(), json!(source.as_str()));
        }
        Ok((!options.is_empty()).then_some(JsonValue::Object(options)))
    }
}

/// What a journaled agent call returned.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentOutcome {
    /// The run's final text, else the text of its last response; the
    /// compact JSON of the answer when the run had an output schema.
    pub text: String,
    /// The checked structured answer, when the run had an output schema.
    pub structured: Option<JsonObject>,
    /// Tokens the run spent, all four usage buckets summed.
    pub tokens: u64,
    /// The priced cost of the run in USD.
    pub cost_usd: f64,
    /// The session the call's run ran in; a later call can `fork` it to
    /// continue from the same conversation.
    pub session_id: SessionId,
    /// Whether the journal answered, so no run was made.
    pub replayed: bool,
}

/// The value an agent call journals.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Stored {
    text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    structured: Option<JsonObject>,
    #[serde(default)]
    tokens: u64,
    #[serde(default)]
    cost_usd: f64,
    #[serde(default)]
    session_id: String,
}

impl WorkflowContext {
    /// Run `agent` on `prompt` as a journaled call named `name` and return
    /// its final text.
    ///
    /// The same as [`agent_with`](Self::agent_with) with no options.
    pub async fn agent(
        &self,
        agent: &Agent,
        name: &str,
        prompt: &str,
    ) -> Result<String, WorkflowError> {
        let call = AgentCall::new(name, prompt);
        Ok(self.agent_with(agent, call).await?.text)
    }

    /// Run `agent` as the journaled call `call` and return what it
    /// produced.
    ///
    /// Each call is its own run of `agent`, on a new session, with a run id
    /// derived from the workflow, the call's key (see
    /// [`agent_key_with`](super::agent_key_with), plus its branch path
    /// inside a fan-out) and its occurrence. So when a process crashes
    /// during the call, the next [`run_workflow`](super::run_workflow)
    /// continues that run instead of starting over, and never pays a model
    /// call twice (the run's own journal sees to that). The result is
    /// journaled once the run succeeds, with what it spent; a resume after
    /// that replays it without touching the agent.
    ///
    /// Before a call runs live, the workflow's budget is checked against
    /// what its finished calls spent (replayed ones included).
    ///
    /// Once the run has ended, the call closes its session
    /// ([`Agent::close_session`]), which frees what the agent holds for it
    /// in this process: after a success once the result is journaled, and
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
    /// [`Agent::fail`]. A run another process still holds the lease of
    /// (a runner that crashed less than a lease ago) is waited for, until
    /// its lease runs out or the call is cancelled. The workflow's
    /// workflow cancel token aborts the run; the workflow deadline applies,
    /// and the call's own `timeout` and `retry` apply to each attempt.
    ///
    /// A `fork` whose destination session already exists with no run (a
    /// crash between the copy's create and its append) starts the next
    /// generation instead of reusing a session that may be empty.
    ///
    /// Args:
    ///     agent: The agent to run; its namespace and stores hold the run.
    ///     call: The call's name, prompt and options.
    ///
    /// Returns:
    ///     The run's result, or the journaled one.
    ///
    /// Raises:
    ///     WorkflowError: `Aborted` when the cancel token fired before the
    ///         call or the run was aborted; `Timeout` past the call's
    ///         limit; `BudgetExceeded` when the budget is spent; `Step`
    ///         when the run failed, is waiting for an approval, or could
    ///         not be read or continued; `Store` when the journal write
    ///         fails.
    pub async fn agent_with(
        &self,
        agent: &Agent,
        call: AgentCall,
    ) -> Result<AgentOutcome, WorkflowError> {
        let name = call.name.as_str();
        let label = format!("agent {name}");
        let options = call
            .options()
            .map_err(|error| encode_error(&label, format!("its options do not encode: {error}")))?;
        let content_key = agent_key_with(name, &call.prompt, call.key.as_deref(), options.as_ref())
            .map_err(|error| encode_error(&label, format!("its key does not encode: {error}")))?;
        let (call_key, occurrence, cached) = self.next_occurrence(&content_key);
        let title = call.label.as_deref().unwrap_or(name);
        let emit = |kind, result_text: Option<&str>| {
            let mut event = event(kind, title.to_owned(), call_key.clone(), occurrence);
            event.subagent_type = name.to_owned();
            event.result_text = result_text.map(str::to_owned);
            self.emit(event);
        };
        if let Some(value) = cached {
            let stored: Stored = decode(&label, value)?;
            self.charge(stored.tokens, stored.cost_usd);
            emit(WorkflowKind::AgentReplayed, Some(&stored.text));
            return Ok(AgentOutcome {
                text: stored.text,
                structured: stored.structured,
                tokens: stored.tokens,
                cost_usd: stored.cost_usd,
                session_id: SessionId::new(stored.session_id),
                replayed: true,
            });
        }
        self.check_cancel(&label)?;
        self.check_budget()?;
        self.check_room()?;
        let _live = self.live_guard()?;
        emit(WorkflowKind::AgentStart, None);
        let token = (call.cancel.clone()).unwrap_or_else(|| self.inner.cancel.clone());
        let retry = call.retry.as_ref();
        let max_attempts = retry.map_or(1, |retry| retry.max_attempts.max(1));
        let mut failed = 0;
        let (run_id, finished) = loop {
            let attempt = self.attempt(agent, &call, (&call_key, occurrence), &token, &label);
            let outcome = attempt.await;
            let retried = matches!(
                outcome,
                Err(WorkflowError::Step { .. } | WorkflowError::Timeout { .. })
            );
            let Some(retry) = retry.filter(|_| retried && failed + 1 < max_attempts) else {
                break outcome?;
            };
            tokio::time::sleep(retry.delay(failed)).await;
            failed += 1;
            self.check_cancel(&label)?;
            if token.is_cancelled() {
                return Err(WorkflowError::Aborted { label });
            }
        };
        let stored = Stored {
            text: finished.text.clone(),
            structured: finished.structured.clone(),
            tokens: finished.tokens,
            cost_usd: finished.cost_usd,
            session_id: run_id.as_str().to_owned(),
        };
        let value = serde_json::to_value(&stored)
            .map_err(|error| encode_error(&label, format!("its value does not encode: {error}")))?;
        self.record(&call_key, occurrence, &value).await?;
        self.charge(finished.tokens, finished.cost_usd);
        agent.close_session(&SessionId::new(run_id.as_str()));
        emit(WorkflowKind::AgentEnd, Some(&stored.text));
        Ok(AgentOutcome {
            text: stored.text,
            structured: stored.structured,
            tokens: stored.tokens,
            cost_usd: stored.cost_usd,
            session_id: SessionId::new(stored.session_id),
            replayed: false,
        })
    }

    /// Ask the host's gate, if it set one, to let a live call start.
    fn live_guard(&self) -> Result<Option<Box<dyn std::any::Any + Send + Sync>>, WorkflowError> {
        match &self.inner.live_gate {
            Some(gate) => gate().map(Some),
            None => Ok(None),
        }
    }

    /// Add what a finished call spent to the workflow's total.
    fn charge(&self, tokens: u64, usd: f64) {
        let mut spent = lock(&self.inner.spent);
        spent.0 = spent.0.saturating_add(tokens);
        spent.1 += usd;
    }

    /// `BudgetExceeded` when the workflow's budget is spent.
    fn check_budget(&self) -> Result<(), WorkflowError> {
        let Some(budget) = self.inner.budget else {
            return Ok(());
        };
        let (tokens, usd) = self.spent();
        let spent = (budget.max_tokens.is_some_and(|max| tokens >= max))
            || (budget.max_cost_usd.is_some_and(|max| usd >= max));
        if spent {
            return Err(WorkflowError::BudgetExceeded {
                spent_tokens: tokens,
                spent_usd: usd,
                max_tokens: budget.max_tokens,
                max_cost_usd: budget.max_cost_usd,
            });
        }
        Ok(())
    }

    /// One attempt of a call: its run, cut off at the call's timeout.
    async fn attempt(
        &self,
        agent: &Agent,
        call: &AgentCall,
        (call_key, occurrence): (&str, u64),
        token: &CancellationToken,
        label: &str,
    ) -> Result<(RunId, Finished), WorkflowError> {
        // A timeout aborts this attempt's run only, not a retry's.
        let attempt_token = token.child_token();
        let run = self.run_generations(agent, call, (call_key, occurrence), &attempt_token, label);
        let Some(after) = call.timeout.filter(|timeout| !timeout.is_zero()) else {
            return run.await;
        };
        let mut run = pin!(run);
        let timer = pin!(tokio::time::sleep(after));
        match select(run.as_mut(), timer).await {
            Either::Left((outcome, _)) => outcome,
            Either::Right(_) => {
                // The run ends aborted and closes its session; wait for it.
                attempt_token.cancel();
                let _ = run.await;
                Err(WorkflowError::Timeout {
                    label: label.to_owned(),
                    after,
                })
            }
        }
    }

    /// Start, continue or restart the run of one attempt, and read how it
    /// ended.
    async fn run_generations(
        &self,
        agent: &Agent,
        call: &AgentCall,
        (call_key, occurrence): (&str, u64),
        token: &CancellationToken,
        label: &str,
    ) -> Result<(RunId, Finished), WorkflowError> {
        let name = call.name.as_str();
        let lease = &self.inner.lease;
        let mut generation = 0;
        let (run_id, outcome) = loop {
            let run_id = agent_run_id(
                &lease.namespace,
                &lease.workflow_id,
                call_key,
                occurrence,
                generation,
            );
            let session_id = SessionId::new(run_id.as_str());
            let options = RunOptions {
                session_id: session_id.clone(),
                run_id: Some(run_id.clone()),
                principal: self.inner.principal.clone(),
                owner: "workflow".into(),
                cancel: token.child_token(),
                trace_id: None,
                output_schema: call.output_schema.clone(),
                tool_choice: None,
                schedule: None,
            };
            let stream = match agent.events(&run_id, 0).await {
                Err(RunError::Store(StoreError::NotFound { .. })) => {
                    if let Some(source) = &call.fork {
                        match agent.fork_session(source, &session_id, None).await {
                            Ok(_) => {}
                            // An earlier attempt created the session and
                            // never wrote the run: it may hold half a
                            // copy, so start over on the next generation.
                            Err(ForkError::Exists(_)) => {
                                generation += 1;
                                continue;
                            }
                            Err(error) => {
                                let error = format!("fork of session {source}: {error}");
                                break (run_id, Err(error));
                            }
                        }
                    }
                    agent.run(call.prompt.as_str(), options)
                }
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
                Err(error) => break (run_id, Err(error.to_string())),
            };
            match drive(stream).await {
                // The run's lease is another process's: a runner that
                // crashed holds it until it runs out. Wait, then look again.
                Err(RunError::Store(StoreError::RunLeased { expires_in, .. })) => {
                    let wait = pin!(tokio::time::sleep(expires_in.max(LEASE_POLL)));
                    let cancelled = pin!(token.cancelled());
                    if let Either::Right(_) = select(wait, cancelled).await {
                        self.check_cancel(label)?;
                        return Err(WorkflowError::Aborted {
                            label: label.to_owned(),
                        });
                    }
                }
                driven => break (run_id, driven.map_err(|error| error.to_string())),
            }
        };
        // A run that ended never continues on its session, while a run
        // waiting for an approval does.
        let session = SessionId::new(run_id.as_str());
        let ended = outcome
            .as_ref()
            .is_ok_and(|outcome| outcome.ended.is_some());
        let finished = finish(name, label, &run_id, outcome).inspect_err(|_| {
            if ended {
                agent.close_session(&session);
            }
        })?;
        Ok((run_id, finished))
    }
}

/// The shortest wait before an agent call looks again at a run another
/// process leases.
const LEASE_POLL: Duration = Duration::from_millis(100);

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

/// A successful run's result.
struct Finished {
    text: String,
    structured: Option<JsonObject>,
    tokens: u64,
    cost_usd: f64,
}

/// The call's result, or why it has none.
fn finish(
    name: &str,
    label: &str,
    run_id: &RunId,
    outcome: Result<Outcome, String>,
) -> Result<Finished, WorkflowError> {
    let failed = |message: String| WorkflowError::Step {
        name: name.to_owned(),
        source: message.into(),
    };
    let outcome = outcome.map_err(|error| failed(format!("agent run {run_id}: {error}")))?;
    match outcome.ended {
        Some(ResultSubtype::Success) => {
            let text = match &outcome.structured {
                Some(answer) => JsonValue::Object(answer.clone()).to_string(),
                None => outcome.text,
            };
            Ok(Finished {
                text,
                structured: outcome.structured,
                tokens: outcome.tokens,
                cost_usd: outcome.cost_usd,
            })
        }
        Some(ResultSubtype::Aborted | ResultSubtype::Interrupted) => Err(WorkflowError::Aborted {
            label: label.to_owned(),
        }),
        Some(ResultSubtype::Error) => {
            let error = outcome.error.unwrap_or_else(|| "no error event".into());
            Err(failed(format!("agent run {run_id} failed: {error}")))
        }
        None if outcome.suspended => Err(WorkflowError::AwaitingApproval {
            name: name.to_owned(),
            run_id: run_id.to_string(),
        }),
        None => Err(failed(format!("agent run {run_id} ended without a result"))),
    }
}

/// What a run's events say about how it ended.
#[derive(Default)]
struct Outcome {
    /// The final text, else the last response's text.
    text: String,
    /// The checked structured answer.
    structured: Option<JsonObject>,
    /// The last error event's message.
    error: Option<String>,
    ended: Option<ResultSubtype>,
    suspended: bool,
    tokens: u64,
    cost_usd: f64,
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
                self.structured = result.structured_output.clone();
                let usage = &result.total_usage;
                self.tokens = usage
                    .total_input_tokens()
                    .saturating_add(usage.output_tokens);
                self.cost_usd = result.total_cost_usd.unwrap_or(0.0);
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
