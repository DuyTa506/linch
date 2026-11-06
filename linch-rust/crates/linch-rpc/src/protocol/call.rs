//! Server requests to the client, and their cancellation.

use linch::{JsonObject, Principal, RunId, SessionId};
use serde::{Deserialize, Serialize};

use crate::message::Id;

/// Params of `tool.call`: run host tool `name` on `input` for one tool use.
/// The result is a `ToolOutput`, `{"value": ..., "metadata"?: {...}}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallParams {
    /// Agent handle the tool was declared on.
    pub agent: String,
    /// The tool's name.
    pub name: String,
    /// The input, already validated against the tool's schema.
    pub input: JsonObject,
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use; stable across a retry.
    pub tool_use_id: String,
    /// Caller identity, when the run has one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `$/cancelRequest`: the server stopped waiting for `id`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelRequestParams {
    /// Id of the cancelled server request.
    pub id: Id,
}

/// The `code` of the tool error a host tool call ends with when it does not
/// return a result. The message is always readable by the model.
pub mod tool_code {
    /// The client answered `tool.call` with an error object; its `message`
    /// is the tool error's message and its `data` the details.
    pub const TOOL_ERROR: &str = "host_tool_error";
    /// The client's result is not a `ToolOutput`.
    pub const PROTOCOL_ERROR: &str = "host_protocol_error";
    /// No answer within the tool's timeout; the client got
    /// `$/cancelRequest`.
    pub const TIMEOUT: &str = "timeout";
    /// The run was cancelled; the client got `$/cancelRequest`.
    pub const ABORTED: &str = "aborted";
    /// The connection to the client is closed.
    pub const UNAVAILABLE: &str = "host_unavailable";
}
