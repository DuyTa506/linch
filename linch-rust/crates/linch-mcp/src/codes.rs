//! Codes on the [`ToolOutputError`](linch_core::ToolOutputError)s an MCP tool
//! returns. The message is always readable by the model; the code is for the
//! host.

/// `ToolContext::cancel` fired; the server got `notifications/cancelled`.
pub const ABORTED: &str = "aborted";
/// No result within the call timeout; the server got `notifications/cancelled`.
pub const TIMEOUT: &str = "timeout";
/// The server is reconnecting, unavailable or disconnected, or the
/// connection dropped during the call.
pub const UNAVAILABLE: &str = "mcp_unavailable";
/// The server ran the tool and returned `isError: true`.
pub const TOOL_ERROR: &str = "mcp_tool_error";
/// The server answered with a JSON-RPC error or a response this client does
/// not support (`input_required`, a task).
pub const PROTOCOL_ERROR: &str = "mcp_protocol_error";
