//! Error objects and the code table (see the crate docs).

use std::error::Error;

use linch::mcp::McpError;
use linch::{
    BuildError, JournalError, JsonValue, ModelsError, OpenError, ProviderError, RunError,
    StoreError,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

/// A JSON-RPC error object.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, thiserror::Error)]
#[serde(deny_unknown_fields)]
#[error("{message} (code {code})")]
pub struct ErrorObject {
    /// One of the codes in [`code`].
    pub code: i64,
    /// What went wrong, with its causes.
    pub message: String,
    /// Structured details for some codes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<JsonValue>,
}

/// Error codes: the JSON-RPC 2.0 ones, then this protocol's, all in the
/// implementation-defined range -32000 to -32099.
pub mod code {
    /// The line is not JSON.
    pub const PARSE_ERROR: i64 = -32700;
    /// Not a valid request object, or a batch.
    pub const INVALID_REQUEST: i64 = -32600;
    /// No such method.
    pub const METHOD_NOT_FOUND: i64 = -32601;
    /// Params do not fit the method (unknown or missing fields, bad types).
    pub const INVALID_PARAMS: i64 = -32602;
    /// A bug in the server.
    pub const INTERNAL_ERROR: i64 = -32603;
    /// No open agent has that handle.
    pub const AGENT_NOT_FOUND: i64 = -32001;
    /// The server already drives that run, or `agent.close` named an
    /// agent with a run in progress.
    pub const RUN_BUSY: i64 = -32002;
    /// No valid bearer token: a socket connection whose first message is
    /// not `connection.auth` with a known token. The server then closes
    /// the connection.
    pub const UNAUTHENTICATED: i64 = -32003;
    /// Outside the tenant's allowlists, or naming another tenant;
    /// `data.kind` names the check that refused (the crate docs list
    /// them).
    pub const NOT_ALLOWED: i64 = -32004;
    /// A cap is reached and nothing was started; try again once a run,
    /// agent or connection ends. `data` is `{kind, max}`: `kind` is
    /// `runs`, `agents` or `connections` (the tenant's, across its
    /// connections) or `logins` (socket connections waiting to log in),
    /// `max` the cap. `provider_calls` is a rate, not a count of what is
    /// open: at most `max` `provider.models` and `provider.model` calls
    /// per `per_seconds` for the tenant; try again later.
    pub const LIMIT_REACHED: i64 = -32005;
    /// `BuildError::Missing`: a required binding was not set.
    pub const BUILD_MISSING: i64 = -32010;
    /// `BuildError::Invalid`, or a workspace, policy or tool error.
    pub const BUILD_INVALID: i64 = -32011;
    /// `BuildError::Contract`: the contract could not be digested.
    pub const BUILD_CONTRACT: i64 = -32012;
    /// The provider key is unset (`data.env` names its variable), or the
    /// provider client could not be built.
    pub const PROVIDER_UNAVAILABLE: i64 = -32013;
    /// Postgres could not be reached or migrated.
    pub const STORE_UNAVAILABLE: i64 = -32014;
    /// An MCP server of the config could not be connected; `data.server`
    /// names it.
    pub const MCP_UNAVAILABLE: i64 = -32015;
    /// `RunError::Store`, any store failure not listed below.
    pub const STORE: i64 = -32020;
    /// `StoreError::NotFound`: no such run or session.
    pub const NOT_FOUND: i64 = -32021;
    /// `StoreError::LeaseLost`, from a claim or a journal write: another
    /// claim fenced this run out (another process took it over, or failed
    /// it). The run stopped here without writing; its new holder drives it.
    pub const LEASE_LOST: i64 = -32022;
    /// `StoreError::Conflict`, for example a chosen run id that is already
    /// stored.
    pub const CONFLICT: i64 = -32023;
    /// `RunError::Journal`: a journal write failed.
    pub const JOURNAL: i64 = -32024;
    /// `RunError::ContractMismatch`; `data.expected` is the contract the
    /// run started under and `data.found` this agent's, in the order the
    /// message names them.
    pub const CONTRACT_MISMATCH: i64 = -32025;
    /// `RunError::Terminal`; `data.status` is the final status.
    pub const TERMINAL: i64 = -32026;
    /// `RunError::NotSuspended`: approve on a run not awaiting approval, or
    /// answer on one not awaiting input; a host that retried an approve or
    /// an answer continues the run with `run.resume`.
    pub const NOT_SUSPENDED: i64 = -32027;
    /// `RunError::Reconcile`: durable state does not fit together.
    pub const RECONCILE: i64 = -32028;
    /// `RunError::PromptBlocked`: a `user_prompt_submit` hook refused the
    /// prompt; `data.reason` says why. Nothing was written.
    pub const PROMPT_BLOCKED: i64 = -32029;
    /// `StoreError::SessionBusy`: the session already has a run that is
    /// not terminal, so this one was not created. `data` is
    /// `{session_id, run_id}`, where `run_id` names that live run: resume,
    /// approve or cancel it first.
    pub const SESSION_BUSY: i64 = -32030;
    /// `RunError::InvalidOutputSchema`: the `output_schema` of `run.start`
    /// does not compile, or its root is not an object; `data.reason` says
    /// why. Nothing was written.
    pub const INVALID_OUTPUT_SCHEMA: i64 = -32031;
    /// `RunError::InvalidAnswer`: a `run.answer` that does not fit the
    /// run's pending input requests; `data.reason` says why. Nothing was
    /// written: the run still awaits input.
    pub const INVALID_ANSWER: i64 = -32032;
    /// `StoreError::RunLeased`: another process drives the run and keeps
    /// its lease alive, so `run.resume`, `run.approve` or `run.answer`
    /// wrote nothing. `data` is `{run_id, owner, expires_in_ms}`: try again
    /// once that holder stops, at the latest after `expires_in_ms`.
    pub const RUN_LEASED: i64 = -32033;
    /// `RunError::InvalidToolChoice`: the `tool_choice` of `run.start` names
    /// a tool the run does not offer, or the agent's provider refuses it;
    /// `data.reason` says why. Nothing was written.
    pub const INVALID_TOOL_CHOICE: i64 = -32034;
    /// `provider.models` or `provider.model` named a provider with no model
    /// list (`scripted`); `data.provider` is its id.
    pub const MODELS_UNSUPPORTED: i64 = -32035;
    /// `provider.model` named a model the provider's list does not hold;
    /// `data.model` is the id asked for, and `data.truncated` is `true`
    /// when the list was cut short, so the model may exist past the cut.
    pub const MODEL_NOT_FOUND: i64 = -32036;
    /// The provider could not be asked, or answered with an error:
    /// `data.kind` is `unreachable` (a refused connection, a reset, a
    /// timeout and any other transport failure alike), `status` (with
    /// `data.status`), `invalid_request` or `other`. The message is one
    /// fixed text per kind: never the key, the provider's URL or the
    /// vendor's own words.
    pub const PROVIDER_REQUEST_FAILED: i64 = -32037;
    /// `RunError::InvalidSteer`: the steer's text is blank or too long, or its
    /// id is not usable; `data.reason` says why. Nothing was written.
    pub const INVALID_STEER: i64 = -32038;
    /// The agent cannot run in the background, so `schedule.create` and
    /// `job.enqueue` refuse it: `data.reason` is `memory_store` (its runs
    /// would live in this connection only), `host_tools`, `callbacks` or
    /// `remote_execution` (they answer on this connection, which a
    /// background run has not).
    pub const NOT_BACKGROUND: i64 = -32040;
    /// The agent's namespace has no schedule with that id.
    pub const SCHEDULE_NOT_FOUND: i64 = -32041;
    /// The agent's namespace already has a schedule with that id.
    pub const SCHEDULE_EXISTS: i64 = -32042;
    /// The agent's namespace has no job with that id (or its history was
    /// pruned).
    pub const JOB_NOT_FOUND: i64 = -32043;
    /// The schedule's timing is unusable: a cron pattern that does not
    /// parse or never fires, an unknown time zone, an interval out of
    /// range, or both or neither of `cron` and `interval_ms`.
    /// `data.reason` says which.
    pub const INVALID_SCHEDULE: i64 = -32044;
    /// Another client drives the workflow (this connection, another
    /// connection or another server): a workflow has one driver at a time.
    /// `workflow.start` wrote nothing; try again once that client
    /// finishes or its connection ends.
    pub const WORKFLOW_HELD: i64 = -32045;
    /// The workflow's budget is spent, so no further agent call starts;
    /// `data` is `{spent_tokens, spent_usd, max_tokens?, max_cost_usd?}`.
    /// The calls that finished stay journaled.
    pub const WORKFLOW_BUDGET: i64 = -32046;
    /// A `workflow.agent` call did not produce a result: its run failed,
    /// was aborted, timed out, waits for an approval, or the subagent is
    /// unknown. `data.kind` is `failed`, `aborted`, `timeout`,
    /// `approval` or `invalid`.
    pub const WORKFLOW_CALL_FAILED: i64 = -32047;
    /// This connection drives no workflow with that id: it never sent
    /// `workflow.start` for it, or already finished it.
    pub const WORKFLOW_NOT_FOUND: i64 = -32048;
    /// `workflow.start` carried `resume` answers for a workflow whose journal
    /// holds nothing: it never ran here, or its journal lived in another
    /// connection (a memory-store agent's does), so it would run again from
    /// its first step with answers that have nothing to answer. Nothing was
    /// claimed; start it without `resume`, or use Postgres stores.
    pub const WORKFLOW_NO_JOURNAL: i64 = -32049;
}

impl ErrorObject {
    /// An error with `code` and `message` and no data.
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }

    pub(crate) fn with_data(mut self, data: JsonValue) -> Self {
        self.data = Some(data);
        self
    }
}

pub(crate) fn invalid_request(message: &str) -> ErrorObject {
    ErrorObject::new(code::INVALID_REQUEST, message)
}

pub(crate) fn invalid_params(message: &str) -> ErrorObject {
    ErrorObject::new(code::INVALID_PARAMS, message)
}

/// The cap on `kind` (`runs`, `agents`, `connections` or `logins`), `max`,
/// is reached.
pub(crate) fn limit_reached(kind: &str, max: usize) -> ErrorObject {
    let message = format!("at most {max} {kind} at once; try again once one ends");
    ErrorObject::new(code::LIMIT_REACHED, message).with_data(json!({ "kind": kind, "max": max }))
}

/// More than `max` calls of `kind` within `per`: `-32005` with
/// `data: {kind, max, per_seconds}`.
pub(crate) fn rate_limited(kind: &str, max: usize, per: std::time::Duration) -> ErrorObject {
    let seconds = per.as_secs();
    let message = format!("at most {max} {kind} per {seconds} s; try again later");
    let data = json!({ "kind": kind, "max": max, "per_seconds": seconds });
    ErrorObject::new(code::LIMIT_REACHED, message).with_data(data)
}

/// `error`'s text followed by each cause not already part of it.
pub(crate) fn chain(error: &dyn Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        let part = cause.to_string();
        if !text.contains(&part) {
            text = format!("{text}: {part}");
        }
        source = cause.source();
    }
    text
}

impl From<&RunError> for ErrorObject {
    fn from(error: &RunError) -> Self {
        let message = chain(error);
        match error {
            RunError::Store(StoreError::SessionBusy { session_id, run_id }) => {
                Self::new(code::SESSION_BUSY, message)
                    .with_data(json!({ "session_id": session_id, "run_id": run_id }))
            }
            RunError::Store(StoreError::RunLeased {
                run_id,
                owner,
                expires_in,
            }) => Self::new(code::RUN_LEASED, message).with_data(json!({
                "run_id": run_id,
                "owner": owner,
                "expires_in_ms": u64::try_from(expires_in.as_millis()).unwrap_or(u64::MAX),
            })),
            RunError::Store(store) => Self::new(store_code(store), message),
            RunError::Journal(JournalError::Store(StoreError::LeaseLost { .. })) => {
                Self::new(code::LEASE_LOST, message)
            }
            RunError::Journal(_) => Self::new(code::JOURNAL, message),
            // The message names the run's contract first, then the agent's;
            // the data reads the same way: the run expected its own contract
            // and found this agent's. Core names them from the agent's side.
            RunError::ContractMismatch {
                expected, found, ..
            } => Self::new(code::CONTRACT_MISMATCH, message).with_data(json!({
                "expected": found.to_string(),
                "found": expected.to_string(),
            })),
            RunError::Terminal { status, .. } => {
                Self::new(code::TERMINAL, message).with_data(json!({ "status": status }))
            }
            RunError::NotSuspended { .. } => Self::new(code::NOT_SUSPENDED, message),
            RunError::Reconcile(_) => Self::new(code::RECONCILE, message),
            RunError::PromptBlocked { reason, .. } => {
                Self::new(code::PROMPT_BLOCKED, message).with_data(json!({ "reason": reason }))
            }
            RunError::InvalidOutputSchema { reason, .. } => {
                Self::new(code::INVALID_OUTPUT_SCHEMA, message)
                    .with_data(json!({ "reason": reason }))
            }
            RunError::InvalidAnswer { reason, .. } => {
                Self::new(code::INVALID_ANSWER, message).with_data(json!({ "reason": reason }))
            }
            RunError::InvalidSteer { reason, .. } => {
                Self::new(code::INVALID_STEER, message).with_data(json!({ "reason": reason }))
            }
            RunError::SteerQueueFull { max, .. } => limit_reached("steers", *max),
            RunError::InvalidToolChoice { reason, .. } => {
                Self::new(code::INVALID_TOOL_CHOICE, message).with_data(json!({ "reason": reason }))
            }
        }
    }
}

fn store_code(error: &StoreError) -> i64 {
    match error {
        StoreError::NotFound { .. } => code::NOT_FOUND,
        StoreError::LeaseLost { .. } => code::LEASE_LOST,
        StoreError::Conflict { .. } => code::CONFLICT,
        _ => code::STORE,
    }
}

impl From<&ModelsError> for ErrorObject {
    fn from(error: &ModelsError) -> Self {
        let message = chain(error);
        match error {
            ModelsError::Unsupported { provider } => Self::new(code::MODELS_UNSUPPORTED, message)
                .with_data(json!({ "provider": provider })),
            ModelsError::NotFound { model, truncated } => {
                // A cut list may hold the model past the cut.
                let data = match truncated {
                    true => json!({ "model": model, "truncated": true }),
                    false => json!({ "model": model }),
                };
                Self::new(code::MODEL_NOT_FOUND, message).with_data(data)
            }
            // One fixed text per kind, so an answer never tells a closed
            // port from a slow host, and never carries the vendor's words.
            ModelsError::Provider(error) => {
                let (message, data) = match error {
                    ProviderError::Connect { .. } | ProviderError::Transport { .. } => (
                        "the provider could not be reached",
                        json!({ "kind": "unreachable" }),
                    ),
                    ProviderError::Status { status, .. } => (
                        "the provider answered with an error status",
                        json!({ "kind": "status", "status": status }),
                    ),
                    ProviderError::InvalidRequest { .. } => (
                        "the provider request is not valid",
                        json!({ "kind": "invalid_request" }),
                    ),
                    _ => ("the provider request failed", json!({ "kind": "other" })),
                };
                Self::new(code::PROVIDER_REQUEST_FAILED, message).with_data(data)
            }
        }
    }
}

impl From<&OpenError> for ErrorObject {
    fn from(error: &OpenError) -> Self {
        let message = chain(error);
        match error {
            OpenError::MissingKey { env, .. } => {
                Self::new(code::PROVIDER_UNAVAILABLE, message).with_data(json!({ "env": env }))
            }
            OpenError::Provider(_) => Self::new(code::PROVIDER_UNAVAILABLE, message),
            OpenError::Workspace(_)
            | OpenError::Execution(_)
            | OpenError::Tools(_)
            | OpenError::Policy(_)
            | OpenError::DeepAgent(_)
            | OpenError::Profile(_)
            | OpenError::Memory(_)
            | OpenError::ScheduleTools(_)
            | OpenError::TaskLedger(_) => Self::new(code::BUILD_INVALID, message),
            OpenError::Store(_) => Self::new(code::STORE_UNAVAILABLE, message),
            OpenError::Mcp(McpError::Config(_)) => Self::new(code::BUILD_INVALID, message),
            OpenError::Mcp(McpError::Connect { server, .. }) => {
                Self::new(code::MCP_UNAVAILABLE, message).with_data(json!({ "server": server }))
            }
            OpenError::Build(build) => Self::new(build_code(build), message),
        }
    }
}

fn build_code(error: &BuildError) -> i64 {
    match error {
        BuildError::Missing(_) => code::BUILD_MISSING,
        BuildError::Invalid(_) => code::BUILD_INVALID,
        BuildError::Contract(_) => code::BUILD_CONTRACT,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_leased_run_names_its_holder_and_a_fenced_write_is_lease_lost() {
        let leased = RunError::Store(StoreError::RunLeased {
            run_id: "r1".into(),
            owner: "worker-2".into(),
            expires_in: std::time::Duration::from_millis(12_500),
        });
        let object = ErrorObject::from(&leased);
        assert_eq!(object.code, code::RUN_LEASED);
        let data = object.data.unwrap();
        assert_eq!(
            (&data["run_id"], &data["owner"], &data["expires_in_ms"]),
            (&json!("r1"), &json!("worker-2"), &json!(12_500))
        );
        let fenced = RunError::Journal(JournalError::Store(StoreError::LeaseLost {
            run_id: "r1".into(),
            epoch: 1,
        }));
        assert_eq!(ErrorObject::from(&fenced).code, code::LEASE_LOST);
        assert_eq!(
            ErrorObject::from(&RunError::Journal(JournalError::Poisoned)).code,
            code::JOURNAL
        );
    }

    #[test]
    fn a_contract_mismatch_names_its_digests_in_the_order_of_its_message() {
        let run = "a".repeat(64);
        let agent = "b".repeat(64);
        let error = RunError::ContractMismatch {
            run_id: "r1".into(),
            expected: agent.parse().unwrap(),
            found: run.parse().unwrap(),
        };
        let object = ErrorObject::from(&error);
        assert_eq!(
            object.message,
            format!("run r1 has contract {run}, this agent has {agent}")
        );
        let data = object.data.unwrap();
        assert_eq!(
            data["expected"], run,
            "the run's contract, which a resume needs"
        );
        assert_eq!(data["found"], agent, "the contract this agent has");
    }
}
