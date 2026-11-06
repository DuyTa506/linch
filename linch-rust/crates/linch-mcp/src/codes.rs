//! Codes on the [`ToolOutputError`](linch_core::ToolOutputError)s an MCP tool
//! returns. The message is always readable by the model; the code is for the
//! host.
//!
//! A call that ends without the server's result, with [`ABORTED`], with
//! [`TIMEOUT`], or because the caller dropped its future (the scheduler's
//! call timeout), is cancelled on the server: `notifications/cancelled` is
//! sent in the background, or over streamable HTTP the request's stream is
//! closed.

/// `ToolContext::cancel` fired; the server is sent `notifications/cancelled`.
pub const ABORTED: &str = "aborted";
/// No result within the call timeout; the server is sent
/// `notifications/cancelled`.
pub const TIMEOUT: &str = "timeout";
/// The server is reconnecting, unavailable or disconnected, the connection
/// dropped during the call, or the call's request could not be sent (an
/// HTTP error status on its POST).
pub const UNAVAILABLE: &str = "mcp_unavailable";
/// The server ran the tool and returned `isError: true`.
pub const TOOL_ERROR: &str = "mcp_tool_error";
/// The server answered with a JSON-RPC error or a response this client does
/// not support (`input_required`, a task).
pub const PROTOCOL_ERROR: &str = "mcp_protocol_error";
