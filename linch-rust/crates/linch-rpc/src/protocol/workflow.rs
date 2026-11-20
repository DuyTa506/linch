//! Params and results of the workflow methods: `workflow.*`.
//!
//! A workflow function runs in the client; the server holds its journal.
//! Every call that must replay (`workflow.agent`, a step's two halves,
//! `workflow.interrupt`) names the workflow by `run_id` and, inside a
//! client-side fan-out, the branch it runs in.

use std::collections::BTreeMap;

use linch::ext::workflow::Interrupt;
use linch::{JsonValue, OutputSchema, Principal, SessionId};
use serde::{Deserialize, Serialize};

/// Most bytes of a workflow id; it is part of the journal key and of the
/// ids of the runs the workflow's agent calls make.
pub const MAX_WORKFLOW_ID_BYTES: usize = 256;

/// Fewest distinct call keys a tenant's workflow may keep counters for in
/// one drive, whatever its `max_records` (a larger `max_records` raises it
/// to that): a call that failed or suspended journals nothing but keeps its
/// key, which is a few hundred bytes. Past it a call with a new key is
/// refused (`-32005`, `kind: call_keys`); replays are never refused.
///
/// The same number bounds the steps a tenant's workflow has begun with
/// `workflow.step.begin` and not completed or abandoned, which the server
/// keeps a ticket for: past it `workflow.step.begin` is refused (`-32005`,
/// `kind: pending_steps`). A local connection has neither bound.
pub const MAX_CALL_KEYS: usize = 10_000;

/// A spending cap shared by every agent call of one workflow.
#[derive(Debug, Clone, Copy, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowBudget {
    /// Most tokens the workflow's agent calls may spend, all four usage
    /// buckets summed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u64>,
    /// Most USD they may spend, by the agent's pricing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_cost_usd: Option<f64>,
}

/// Params of `workflow.start`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStartParams {
    /// Agent handle from `agent.open`: the agent every `workflow.agent`
    /// call runs, and whose stores hold the journal (Postgres for a
    /// durable one).
    pub agent: String,
    /// The workflow's id, unique in the agent's namespace; 1 to
    /// [`MAX_WORKFLOW_ID_BYTES`] bytes. Starting an id the namespace
    /// already holds resumes it.
    pub run_id: String,
    /// Caps what the workflow's agent calls spend; absent: no cap beyond
    /// the tenant's.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub budget: Option<WorkflowBudget>,
    /// Answers to interrupts the workflow left pending, by interrupt key;
    /// each is used once, by the first interrupt of that key, and journaled.
    /// An answer applies only to the question the workflow was parked on
    /// (`workflow.status` names it): the key and an equal payload. A
    /// workflow whose journal holds nothing is refused (`-32049`).
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub resume: BTreeMap<String, JsonValue>,
    /// Who the workflow's agent calls act for: the principal of every
    /// call's run, so permission rules keyed on its roles apply, checked
    /// against the tenant as for `run.start`. It is stored with the
    /// workflow; starting an existing id with another principal (or none,
    /// or one where it had none) is refused with `-32004`
    /// (`data.kind` `workflow_principal`) and changes nothing. Absent: the
    /// calls act for nobody.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// What a tenant's allowlists hold one workflow to; absent fields are
/// uncapped.
#[derive(Debug, Clone, Copy, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowLimits {
    /// Most agent calls in flight at once.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_fan_out: Option<usize>,
    /// Most journal records.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_records: Option<u64>,
}

/// Result of `workflow.start`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStarted {
    /// Journal records the workflow already holds.
    pub records: u64,
    /// Whether the workflow existed: its finished calls replay.
    pub resumed: bool,
    /// The status it had before this start: `running` (a crash, or a
    /// driver that is gone), `awaiting_input`, `completed` or `failed`;
    /// absent for a new workflow.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub previous: Option<String>,
    /// The caps the tenant's allowlists put on it; absent for a local
    /// client.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limits: Option<WorkflowLimits>,
}

/// A retry policy for a failed agent call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WireRetry {
    /// Most attempts, the first one included.
    pub max_attempts: u32,
    /// Wait before the first retry, doubling each time; 1000 when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_delay_ms: Option<u64>,
    /// Longest wait between two attempts; 30000 when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_delay_ms: Option<u64>,
}

/// Params of `workflow.agent`: one journaled agent call. The server runs
/// the agent on the prompt as a child run in a session of its own and
/// answers when it ends (or from the journal, without a run).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowAgentParams {
    /// The workflow, as for `workflow.start`.
    pub run_id: String,
    /// The prompt of the run.
    pub prompt: String,
    /// The call's name, in its journal key and on its events; the
    /// subagent's name, else `agent`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// Run this subagent definition of the agent instead of the agent
    /// itself: its tools, its prompt. Part of the key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subagent: Option<String>,
    /// The `title` of the call's events; not part of the key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    /// A caller-owned identity in place of the prompt and options: the
    /// call replays while it stays the same, whatever else changes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,
    /// The output schema of this call's run, as for `run.start`; the
    /// answer is the compact JSON of the checked object. Part of the key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<OutputSchema>,
    /// Start the run on a copy of this session's durable log (a session a
    /// previous call returned as `session_id`, say), so it continues the
    /// same conversation. Part of the key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fork: Option<SessionId>,
    /// Limit on each attempt, in milliseconds; it aborts the run. Absent
    /// or 0: none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<u64>,
    /// Retries after a failed or timed out attempt, each a fresh run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry: Option<WireRetry>,
    /// The client-side fan-out branch the call runs in: the branch index
    /// at each level, outermost first, joined by `.` (`1`, `0.2`); absent
    /// outside every branch. Branches count their calls apart.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
    /// Names the call, for `workflow.cancel`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_id: Option<String>,
}

/// Result of `workflow.agent`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowAgentResult {
    /// The run's final text; with an output schema, the compact JSON of
    /// the checked answer.
    pub text: String,
    /// The checked answer, with an output schema.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured: Option<serde_json::Map<String, JsonValue>>,
    /// The session the run ran in; pass it as a later call's `fork`.
    pub session_id: SessionId,
    /// Tokens the run spent.
    pub tokens: u64,
    /// The priced cost of the run in USD.
    pub cost_usd: f64,
    /// Whether the journal answered, so no run was made.
    pub replayed: bool,
}

/// Params of `workflow.step.begin`: the first half of a journaled step
/// whose function runs in the client.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStepBeginParams {
    /// The workflow.
    pub run_id: String,
    /// The step's durable identity.
    pub name: String,
    /// The identity of the step's inputs, so one name over different
    /// inputs is journaled separately; compared in canonical JSON form.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub key: Option<JsonValue>,
    /// The fan-out branch, as for `workflow.agent`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
}

/// Result of `workflow.step.begin`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStepBegun {
    /// Whether an earlier run finished the step: `value` is its result
    /// and the function must not run.
    pub replayed: bool,
    /// The journaled value of a replayed step.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<JsonValue>,
    /// What to pass to `workflow.step.complete` once the function ran;
    /// absent for a replayed step.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ticket: Option<String>,
}

/// Params of `workflow.step.abandon`: give back a ticket whose function
/// failed in the client, so the step is not pending any more. The step
/// journals nothing and runs again on the next drive.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStepAbandonParams {
    /// The workflow.
    pub run_id: String,
    /// The ticket `workflow.step.begin` gave.
    pub ticket: String,
}

/// Result of `workflow.step.abandon`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStepAbandoned {
    /// Whether the ticket was pending: `false` for one already completed or
    /// abandoned, or never given, so giving a ticket back twice is harmless.
    pub abandoned: bool,
}

/// Params of `workflow.step.complete`. The result is `{}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStepCompleteParams {
    /// The workflow.
    pub run_id: String,
    /// The ticket `workflow.step.begin` gave.
    pub ticket: String,
    /// The step's value; durable in the journal when the call returns.
    pub value: JsonValue,
}

/// Params of `workflow.interrupt`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowInterruptParams {
    /// The workflow.
    pub run_id: String,
    /// The interrupt's durable identity, and the key of its answer.
    pub key: String,
    /// Context for whoever answers; it does not affect replay.
    #[serde(default)]
    pub payload: JsonValue,
    /// The fan-out branch, as for `workflow.agent`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
}

/// Result of `workflow.interrupt`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowInterrupted {
    /// Whether the workflow must stop here: no answer is journaled or
    /// given in `resume`. End it with `workflow.finish`; start it again
    /// with the answer in `resume`.
    pub suspended: bool,
    /// The answer, when there is one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<JsonValue>,
}

/// Params of `workflow.cancel`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowCancelParams {
    /// The workflow.
    pub run_id: String,
    /// The `call_id` of a `workflow.agent` call in flight: its run is
    /// aborted and the call fails; nothing is journaled. Absent: every
    /// call of the workflow.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_id: Option<String>,
}

/// Result of `workflow.cancel`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowCancelled {
    /// How many calls in flight were cancelled.
    pub cancelled: u64,
}

/// Params of `workflow.finish`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowFinishParams {
    /// The workflow.
    pub run_id: String,
    /// Whether the workflow function returned a value; `false` for a
    /// failure, a timeout or a cancel. An interrupt that suspended the
    /// workflow wins over either.
    pub ok: bool,
}

/// Result of `workflow.finish`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowFinished {
    /// The stored status: `completed`, `failed` or `awaiting_input`.
    pub status: String,
}

/// Params of `workflow.status`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStatusParams {
    /// Agent handle; the workflow is looked up in its namespace.
    pub agent: String,
    /// The workflow.
    pub run_id: String,
}

/// Result of `workflow.status`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowStatus {
    /// `running`, `awaiting_input`, `completed` or `failed`.
    pub status: String,
    /// Journal records.
    pub records: u64,
    /// The question a workflow awaiting input is parked at.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interrupt: Option<Interrupt>,
}

/// Params of the `workflow.event` notification: one event of a workflow
/// this connection drives, in order, sent before the answer of the call
/// that made it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowEventParams {
    /// The workflow.
    pub run_id: String,
    /// A `WorkflowEvent`: `kind`, `title`, `call_key`, `occurrence`, ...
    pub event: linch::events::kinds::WorkflowEvent,
}
