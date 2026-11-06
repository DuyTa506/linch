//! Error objects and the code table (see the crate docs).

use std::error::Error;

use linch::{BuildError, JsonValue, OpenError, RunError, StoreError};
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
    /// The server already drives that run.
    pub const RUN_BUSY: i64 = -32002;
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
    /// `RunError::Store`, any store failure not listed below.
    pub const STORE: i64 = -32020;
    /// `StoreError::NotFound`: no such run or session.
    pub const NOT_FOUND: i64 = -32021;
    /// `StoreError::LeaseLost`: another claim fenced this run out.
    pub const LEASE_LOST: i64 = -32022;
    /// `StoreError::Conflict`, for example two runs on one session.
    pub const CONFLICT: i64 = -32023;
    /// `RunError::Journal`: a journal write failed.
    pub const JOURNAL: i64 = -32024;
    /// `RunError::ContractMismatch`; `data` has `expected` and `found`.
    pub const CONTRACT_MISMATCH: i64 = -32025;
    /// `RunError::Terminal`; `data.status` is the final status.
    pub const TERMINAL: i64 = -32026;
    /// `RunError::NotSuspended`: approve on a run not awaiting approval.
    pub const NOT_SUSPENDED: i64 = -32027;
    /// `RunError::Reconcile`: durable state does not fit together.
    pub const RECONCILE: i64 = -32028;
    /// `RunError::PromptBlocked`: a `user_prompt_submit` hook refused the
    /// prompt; `data.reason` says why. Nothing was written.
    pub const PROMPT_BLOCKED: i64 = -32029;
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

    fn with_data(mut self, data: JsonValue) -> Self {
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

/// `error`'s text followed by each cause not already part of it.
fn chain(error: &dyn Error) -> String {
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
            RunError::Store(store) => Self::new(store_code(store), message),
            RunError::Journal(_) => Self::new(code::JOURNAL, message),
            RunError::ContractMismatch {
                expected, found, ..
            } => Self::new(code::CONTRACT_MISMATCH, message).with_data(json!({
                "expected": expected.to_string(),
                "found": found.to_string(),
            })),
            RunError::Terminal { status, .. } => {
                Self::new(code::TERMINAL, message).with_data(json!({ "status": status }))
            }
            RunError::NotSuspended { .. } => Self::new(code::NOT_SUSPENDED, message),
            RunError::Reconcile(_) => Self::new(code::RECONCILE, message),
            RunError::PromptBlocked { reason, .. } => {
                Self::new(code::PROMPT_BLOCKED, message).with_data(json!({ "reason": reason }))
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

impl From<&OpenError> for ErrorObject {
    fn from(error: &OpenError) -> Self {
        let message = chain(error);
        match error {
            OpenError::MissingKey { env, .. } => {
                Self::new(code::PROVIDER_UNAVAILABLE, message).with_data(json!({ "env": env }))
            }
            OpenError::Provider(_) => Self::new(code::PROVIDER_UNAVAILABLE, message),
            OpenError::Workspace(_) | OpenError::Tools(_) | OpenError::Policy(_) => {
                Self::new(code::BUILD_INVALID, message)
            }
            OpenError::Store(_) => Self::new(code::STORE_UNAVAILABLE, message),
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
