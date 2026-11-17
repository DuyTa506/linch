//! The workflow methods: `workflow.start`, `workflow.agent`,
//! `workflow.step.begin`, `workflow.step.complete`, `workflow.interrupt`,
//! `workflow.cancel`, `workflow.finish` and `workflow.status`.
//!
//! The workflow function runs in the client; this connection keeps the
//! [`WorkflowSession`] it drives: the journal's lease and hold, the replay
//! cache, and the occurrence counters of its calls. Each call names the
//! workflow by id and its fan-out branch, so the counters match across
//! resumes whatever order the client's branches reach their calls in.
//!
//! A call is driven like a run: dropped when the connection ends, which
//! leaves its child run at its last durable state for the resume to
//! continue. Only an explicit `workflow.cancel` aborts a child run.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use linch::ext::workflow::{
    AgentCall, JournalStore, JournalStoreError, LiveGate, PgJournalStore, Retry, StepSlot,
    StepTicket, WorkflowBudget as CoreBudget, WorkflowContext, WorkflowError, WorkflowOptions,
    WorkflowSession, WorkflowStatus as CoreStatus,
};
use linch::{Agent, JsonValue};
use serde_json::json;
use tokio::sync::{OnceCell, OwnedSemaphorePermit};
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use super::{Server, lock, to_value};
use crate::access::{Tenant, WorkflowCaps};
use crate::error::{ErrorObject, chain, code, invalid_params};
use crate::message::Notification;
use crate::protocol::{
    MAX_WORKFLOW_ID_BYTES, WorkflowAgentParams, WorkflowAgentResult, WorkflowBudget,
    WorkflowCancelParams, WorkflowCancelled, WorkflowEventParams, WorkflowFinishParams,
    WorkflowFinished, WorkflowInterruptParams, WorkflowInterrupted, WorkflowLimits,
    WorkflowStartParams, WorkflowStarted, WorkflowStatus, WorkflowStatusParams,
    WorkflowStepBeginParams, WorkflowStepBegun, WorkflowStepCompleteParams, method,
};

/// The journal an agent's workflows use: its Postgres database (migrated
/// once), else this connection's memory.
pub(super) type Journals = OnceCell<Arc<dyn JournalStore>>;

/// A workflow this connection drives.
pub(super) struct Driven {
    /// Handle of the agent its calls run.
    pub(super) agent_handle: String,
    agent: Agent,
    session: WorkflowSession,
    /// Aborts every call of the workflow.
    cancel: CancellationToken,
    /// The calls in flight, by the id the client gave them.
    calls: Mutex<HashMap<String, CancellationToken>>,
    next_call: AtomicU64,
    /// Steps begun and not completed.
    tickets: Mutex<HashMap<String, StepTicket>>,
    next_ticket: AtomicU64,
    /// Held by each call in flight, so a finish waits for them.
    inflight: TaskTracker,
    /// Held while the workflow is driven: the tenant's place.
    _place: Option<OwnedSemaphorePermit>,
}

impl Driven {
    fn context(&self) -> &WorkflowContext {
        self.session.context()
    }

    /// End every call in flight and wait for it to unwind.
    async fn quiesce(&self) {
        self.cancel.cancel();
        self.inflight.close();
        self.inflight.wait().await;
    }

    /// Free the workflow's hold once nothing else uses the session.
    async fn release(self: Arc<Self>) {
        if let Ok(driven) = Arc::try_unwrap(self) {
            driven.session.release().await;
        }
    }
}

/// A call in flight: registered for `workflow.cancel`, removed when it ends.
struct InFlight<'a> {
    driven: &'a Driven,
    id: String,
    _token: tokio_util::task::task_tracker::TaskTrackerToken,
}

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        lock(&self.driven.calls).remove(&self.id);
    }
}

/// What a live agent call holds: its count among the workflow's calls in
/// flight, and the tenant's place for a live run.
struct Live {
    count: Arc<AtomicUsize>,
    _place: Option<OwnedSemaphorePermit>,
}

impl Drop for Live {
    fn drop(&mut self) {
        self.count.fetch_sub(1, Ordering::SeqCst);
    }
}

impl Server {
    pub(super) async fn start_workflow(
        &self,
        params: WorkflowStartParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let run_id = workflow_id(&params.run_id)?;
        self.admit_principal(params.principal.as_ref())?;
        let caps = match &self.0.tenant {
            Some(tenant) => Some(tenant.workflow_caps()?),
            None => None,
        };
        let budget = admit_budget(params.budget.unwrap_or_default(), caps.as_ref())?;
        let (agent, store, namespace) = self.workflow_store(&params.agent).await?;
        if lock(&self.0.tables).workflows.contains_key(&run_id) {
            return Err(held(&run_id));
        }
        let place = match &self.0.tenant {
            Some(tenant) => Some(tenant.workflow_place()?),
            None => None,
        };
        let cancel = CancellationToken::new();
        let mut options = WorkflowOptions::new(store, namespace, run_id.clone());
        options.resume = params.resume.into_iter().collect();
        options.budget = budget;
        options.principal = params.principal;
        options.cancel = cancel.clone();
        let outbox = self.0.outbox.clone();
        let event_run = run_id.clone();
        options.on_event = Some(Arc::new(move |event| {
            let params = WorkflowEventParams {
                run_id: event_run.clone(),
                event: event.clone(),
            };
            if let Ok(params) = serde_json::to_value(&params) {
                outbox.send_now(&Notification::new(method::WORKFLOW_EVENT, params));
            }
        }));
        if let Some(caps) = &caps {
            options.max_records = Some(caps.max_records);
            options.max_record_bytes = Some(caps.max_record_bytes);
        }
        options.live_gate = Some(live_gate(self.0.tenant.clone(), caps));
        let session = WorkflowSession::open_held(&options)
            .await
            .map_err(|error| self.workflow_error(&error))?;
        let started = WorkflowStarted {
            records: session.stored() as u64,
            resumed: session.previous().is_some(),
            previous: session.previous().map(|status| status.name().to_owned()),
            limits: caps.map(|caps| WorkflowLimits {
                max_fan_out: Some(caps.max_fan_out),
                max_records: Some(caps.max_records),
            }),
        };
        let driven = Arc::new(Driven {
            agent_handle: params.agent,
            agent,
            session,
            cancel,
            calls: Mutex::default(),
            next_call: AtomicU64::new(0),
            tickets: Mutex::default(),
            next_ticket: AtomicU64::new(0),
            inflight: TaskTracker::new(),
            _place: place,
        });
        lock(&self.0.tables).workflows.insert(run_id, driven);
        to_value(&started)
    }

    pub(super) async fn workflow_agent(
        &self,
        params: WorkflowAgentParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = self.driven(&params.run_id)?;
        let (token, _in_flight) = self.begin_call(&driven, params.call_id)?;
        let subagent = params.subagent.as_deref();
        let agent = match subagent {
            Some(name) => driven.agent.subagent(Some(name)).map_err(|error| {
                let message = format!("subagent {name:?} is not available: {error}");
                ErrorObject::new(code::WORKFLOW_CALL_FAILED, message)
                    .with_data(json!({ "kind": "invalid" }))
            })?,
            None => driven.agent.clone(),
        };
        let name = (params.name.as_deref().or(subagent)).unwrap_or("agent");
        let mut call = AgentCall::new(name, params.prompt);
        call.label = params.label;
        call.key = params.key;
        call.identity = subagent.map(|name| json!({ "subagent": name }));
        call.output_schema = params.output_schema;
        call.fork = params.fork;
        call.timeout = params.timeout_ms.map(Duration::from_millis);
        call.retry = params.retry.map(|retry| Retry {
            max_attempts: retry.max_attempts,
            base_delay: Duration::from_millis(retry.base_delay_ms.unwrap_or(1_000)),
            max_delay: Duration::from_millis(retry.max_delay_ms.unwrap_or(30_000)),
        });
        call.cancel = Some(token);
        let context = driven.context();
        let path = params.branch.unwrap_or_default();
        let calling = context.scoped(&path, context.agent_with(&agent, call));
        // Dropped with the connection, like a run: the child run keeps its
        // last durable state, and a resume continues it.
        let outcome = tokio::select! {
            biased;
            () = self.0.shutdown.cancelled() => return Err(shutting_down()),
            outcome = calling => outcome,
        };
        let outcome = outcome.map_err(|error| self.workflow_error(&error))?;
        to_value(&WorkflowAgentResult {
            text: outcome.text,
            structured: outcome.structured,
            session_id: outcome.session_id,
            tokens: outcome.tokens,
            cost_usd: outcome.cost_usd,
            replayed: outcome.replayed,
        })
    }

    pub(super) async fn workflow_step_begin(
        &self,
        params: WorkflowStepBeginParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = self.driven(&params.run_id)?;
        let context = driven.context();
        let path = params.branch.unwrap_or_default();
        let name = params.name;
        let key = params.key;
        let begun = context
            .scoped(&path, async { context.step_begin(&name, key.as_ref()) })
            .await
            .map_err(|error| self.workflow_error(&error))?;
        let begun = match begun {
            StepSlot::Replayed(value) => WorkflowStepBegun {
                replayed: true,
                value: Some(value),
                ticket: None,
            },
            StepSlot::Run(ticket) => {
                let id = driven.next_ticket.fetch_add(1, Ordering::Relaxed) + 1;
                let id = format!("step-{id}");
                lock(&driven.tickets).insert(id.clone(), ticket);
                WorkflowStepBegun {
                    replayed: false,
                    value: None,
                    ticket: Some(id),
                }
            }
        };
        to_value(&begun)
    }

    pub(super) async fn workflow_step_complete(
        &self,
        params: WorkflowStepCompleteParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = self.driven(&params.run_id)?;
        let ticket = lock(&driven.tickets).remove(&params.ticket);
        let Some(ticket) = ticket else {
            let message = format!("workflow has no step ticket {:?}", params.ticket);
            return Err(invalid_params(&message));
        };
        let completed = driven.context().step_complete(ticket, params.value);
        completed
            .await
            .map_err(|error| self.workflow_error(&error))?;
        Ok(json!({}))
    }

    pub(super) async fn workflow_interrupt(
        &self,
        params: WorkflowInterruptParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = self.driven(&params.run_id)?;
        let context = driven.context();
        let path = params.branch.unwrap_or_default();
        let asked = context.interrupt::<JsonValue>(&params.key, params.payload);
        let answer = context.scoped(&path, asked).await;
        let interrupted = match answer {
            Ok(value) => WorkflowInterrupted {
                suspended: false,
                value: Some(value),
            },
            Err(WorkflowError::Suspended(_)) => WorkflowInterrupted {
                suspended: true,
                value: None,
            },
            Err(error) => return Err(self.workflow_error(&error)),
        };
        to_value(&interrupted)
    }

    pub(super) fn workflow_cancel(
        &self,
        params: WorkflowCancelParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = self.driven(&params.run_id)?;
        let calls = lock(&driven.calls);
        let tokens: Vec<&CancellationToken> = match &params.call_id {
            Some(id) => calls.get(id).into_iter().collect(),
            None => calls.values().collect(),
        };
        for token in &tokens {
            token.cancel();
        }
        to_value(&WorkflowCancelled {
            cancelled: tokens.len() as u64,
        })
    }

    pub(super) async fn workflow_finish(
        &self,
        params: WorkflowFinishParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let driven = lock(&self.0.tables).workflows.remove(&params.run_id);
        let Some(driven) = driven else {
            return Err(not_driven(&params.run_id));
        };
        driven.quiesce().await;
        let finished = driven.session.finish(params.ok).await;
        let status = match driven.context().suspended() {
            Some(interrupt) => CoreStatus::AwaitingInput(interrupt),
            None if params.ok => CoreStatus::Completed,
            None => CoreStatus::Failed,
        };
        driven.release().await;
        finished.map_err(|error| self.workflow_error(&error))?;
        to_value(&WorkflowFinished {
            status: status.name().to_owned(),
        })
    }

    pub(super) async fn workflow_status(
        &self,
        params: WorkflowStatusParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        let run_id = workflow_id(&params.run_id)?;
        if let Some(tenant) = &self.0.tenant {
            tenant.workflow_caps()?;
        }
        let (_, store, namespace) = self.workflow_store(&params.agent).await?;
        let stored = store
            .load(&namespace, &run_id)
            .await
            .map_err(|error| self.workflow_error(&error.into()))?;
        let Some(stored) = stored else {
            return Err(ErrorObject::new(
                code::NOT_FOUND,
                format!("no workflow {run_id:?} in the agent's namespace"),
            ));
        };
        let interrupt = match &stored.status {
            CoreStatus::AwaitingInput(interrupt) => Some(interrupt.clone()),
            _ => None,
        };
        to_value(&WorkflowStatus {
            status: stored.status.name().to_owned(),
            records: stored.records.len() as u64,
            interrupt,
        })
    }

    /// End every workflow this connection still drives, waiting for their
    /// calls to unwind and freeing their holds. Their status stays as it
    /// was, `running`, which reads as the crash it is.
    pub(super) async fn stop_workflows(&self) {
        let driven: Vec<Arc<Driven>> = lock(&self.0.tables)
            .workflows
            .drain()
            .map(|(_, driven)| driven)
            .collect();
        for driven in driven {
            driven.quiesce().await;
            driven.release().await;
        }
    }

    fn driven(&self, run_id: &str) -> Result<Arc<Driven>, ErrorObject> {
        (lock(&self.0.tables).workflows.get(run_id).cloned()).ok_or_else(|| not_driven(run_id))
    }

    /// Register a call in flight: its cancel token (a child of the
    /// workflow's) under the id the client gave it, or one made here.
    fn begin_call<'a>(
        &self,
        driven: &'a Driven,
        id: Option<String>,
    ) -> Result<(CancellationToken, InFlight<'a>), ErrorObject> {
        let id = id.unwrap_or_else(|| {
            let number = driven.next_call.fetch_add(1, Ordering::Relaxed) + 1;
            format!("call-{number}")
        });
        let token = driven.cancel.child_token();
        let mut calls = lock(&driven.calls);
        if calls.contains_key(&id) {
            let message = format!("workflow already has a call {id:?} in flight");
            return Err(invalid_params(&message));
        }
        calls.insert(id.clone(), token.clone());
        let in_flight = InFlight {
            driven,
            id: id.clone(),
            _token: driven.inflight.token(),
        };
        Ok((token, in_flight))
    }

    /// The agent `handle`, the journal its workflows use and its namespace.
    async fn workflow_store(
        &self,
        handle: &str,
    ) -> Result<(Agent, Arc<dyn JournalStore>, String), ErrorObject> {
        let (agent, pool, journals) = {
            let tables = lock(&self.0.tables);
            let opened = tables.opened(handle)?;
            (
                opened.agent.agent().clone(),
                opened.agent.pool().cloned(),
                Arc::clone(&opened.journals),
            )
        };
        let memory = Arc::clone(&self.0.journal);
        let store = journals
            .get_or_try_init(|| async move {
                match pool {
                    Some(pool) => {
                        linch::ext::migrate(&pool).await.map_err(|error| {
                            ErrorObject::new(code::STORE_UNAVAILABLE, chain(&error))
                        })?;
                        Ok::<Arc<dyn JournalStore>, ErrorObject>(Arc::new(PgJournalStore::new(
                            pool,
                        )))
                    }
                    None => Ok(memory as Arc<dyn JournalStore>),
                }
            })
            .await?;
        let namespace = agent.namespace().to_owned();
        Ok((agent, Arc::clone(store), namespace))
    }

    /// The error a workflow call answers with. A tenant never reads the
    /// store's or a run's own error text, which may name hosts or paths.
    fn workflow_error(&self, error: &WorkflowError) -> ErrorObject {
        let tenant = self.0.tenant.is_some();
        match error {
            WorkflowError::Store(JournalStoreError::Held { workflow_id }) => held(workflow_id),
            WorkflowError::Store(JournalStoreError::PrincipalMismatch { workflow_id }) => {
                let message = format!("workflow {workflow_id:?} was started for another principal");
                ErrorObject::new(code::NOT_ALLOWED, message)
                    .with_data(json!({ "kind": "workflow_principal" }))
            }
            WorkflowError::Store(JournalStoreError::LeaseLost { .. }) => {
                ErrorObject::new(code::LEASE_LOST, "another client took the workflow over")
            }
            WorkflowError::Store(JournalStoreError::Conflict { .. }) => ErrorObject::new(
                code::CONFLICT,
                "the journal already holds a record for this call",
            ),
            WorkflowError::Store(error) => {
                tracing::warn!(error = %chain(error), "the workflow journal failed");
                let text = match tenant {
                    true => "the workflow journal failed".to_owned(),
                    false => chain(error),
                };
                ErrorObject::new(code::STORE, text)
            }
            WorkflowError::BudgetExceeded {
                spent_tokens,
                spent_usd,
                max_tokens,
                max_cost_usd,
            } => ErrorObject::new(code::WORKFLOW_BUDGET, error.to_string()).with_data(json!({
                "spent_tokens": spent_tokens,
                "spent_usd": spent_usd,
                "max_tokens": max_tokens,
                "max_cost_usd": max_cost_usd,
            })),
            WorkflowError::LimitExceeded { what, max } => {
                let kind = what.replace(' ', "_");
                let message = format!("the workflow is at its limit of {max} {what}");
                ErrorObject::new(code::LIMIT_REACHED, message)
                    .with_data(json!({ "kind": kind, "max": max }))
            }
            WorkflowError::Aborted { label } => {
                ErrorObject::new(code::WORKFLOW_CALL_FAILED, format!("{label} was aborted"))
                    .with_data(json!({ "kind": "aborted" }))
            }
            WorkflowError::Timeout { label, after } => {
                let message = format!("{label} timed out after {after:?}");
                ErrorObject::new(code::WORKFLOW_CALL_FAILED, message)
                    .with_data(json!({ "kind": "timeout" }))
            }
            WorkflowError::AwaitingApproval { name, run_id } => {
                let message = format!("agent call {name} stopped for an approval");
                ErrorObject::new(code::WORKFLOW_CALL_FAILED, message)
                    .with_data(json!({ "kind": "approval", "run_id": run_id }))
            }
            WorkflowError::Step { name, source } => {
                tracing::warn!(call = %name, error = %source, "a workflow call failed");
                let text = match tenant {
                    true => format!("{name} failed; read its run's events for the cause"),
                    false => error.to_string(),
                };
                ErrorObject::new(code::WORKFLOW_CALL_FAILED, text)
                    .with_data(json!({ "kind": "failed" }))
            }
            WorkflowError::Encode { .. } => invalid_params(&error.to_string()),
            WorkflowError::Suspended(_) => {
                ErrorObject::new(code::INTERNAL_ERROR, "the workflow is suspended")
            }
        }
    }
}

/// `-32045`: another client drives the workflow.
fn held(run_id: &str) -> ErrorObject {
    let message = format!("workflow {run_id:?} is driven by another client");
    ErrorObject::new(code::WORKFLOW_HELD, message).with_data(json!({ "run_id": run_id }))
}

fn not_driven(run_id: &str) -> ErrorObject {
    let message = format!("this connection drives no workflow {run_id:?}; send workflow.start");
    ErrorObject::new(code::WORKFLOW_NOT_FOUND, message).with_data(json!({ "run_id": run_id }))
}

fn shutting_down() -> ErrorObject {
    ErrorObject::new(code::INTERNAL_ERROR, "the connection is closing")
}

fn workflow_id(run_id: &str) -> Result<String, ErrorObject> {
    if run_id.trim().is_empty() || run_id.len() > MAX_WORKFLOW_ID_BYTES {
        let message = format!("run_id must be 1 to {MAX_WORKFLOW_ID_BYTES} bytes and not blank");
        return Err(invalid_params(&message));
    }
    Ok(run_id.to_owned())
}

/// The budget a workflow runs under: the one it asked for, held to the
/// tenant's ceiling, which a workflow that asks for none gets.
fn admit_budget(
    asked: WorkflowBudget,
    caps: Option<&WorkflowCaps>,
) -> Result<Option<CoreBudget>, ErrorObject> {
    let (mut tokens, mut usd) = (asked.max_tokens, asked.max_cost_usd);
    if let Some(caps) = caps {
        let refuse = |what: &str| {
            let message = format!("the workflow's {what} is above the tenant's ceiling");
            ErrorObject::new(code::NOT_ALLOWED, message)
                .with_data(json!({ "kind": "workflow_budget" }))
        };
        if let Some(ceiling) = caps.max_tokens {
            match tokens {
                Some(asked) if asked > ceiling => return Err(refuse("max_tokens")),
                _ => tokens = Some(tokens.unwrap_or(ceiling)),
            }
        }
        if let Some(ceiling) = caps.max_cost_usd {
            match usd {
                Some(asked) if asked > ceiling => return Err(refuse("max_cost_usd")),
                _ => usd = Some(usd.unwrap_or(ceiling)),
            }
        }
    }
    Ok((tokens.is_some() || usd.is_some()).then_some(CoreBudget {
        max_tokens: tokens,
        max_cost_usd: usd,
    }))
}

/// Counts a live agent call among the workflow's calls in flight (held to
/// the tenant's fan-out cap) and takes the tenant's place for the run.
fn live_gate(tenant: Option<Arc<Tenant>>, caps: Option<WorkflowCaps>) -> LiveGate {
    let count = Arc::new(AtomicUsize::new(0));
    Arc::new(move || {
        let before = count.fetch_add(1, Ordering::SeqCst);
        let mut live = Live {
            count: Arc::clone(&count),
            _place: None,
        };
        if let Some(caps) = caps.as_ref().filter(|caps| before >= caps.max_fan_out) {
            return Err(WorkflowError::LimitExceeded {
                what: "agent calls in flight",
                max: caps.max_fan_out as u64,
            });
        }
        if let Some(tenant) = &tenant {
            let place = tenant
                .run_place()
                .map_err(|_| WorkflowError::LimitExceeded {
                    what: "runs",
                    max: tenant.max_runs() as u64,
                })?;
            live._place = Some(place);
        }
        Ok(Box::new(live) as Box<dyn std::any::Any + Send + Sync>)
    })
}
