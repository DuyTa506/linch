//! One MCP tool as a Linch [`Tool`], and the link to its server.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, Tool, ToolContext, ToolOutputError, ToolResult, ToolScope,
};
use rmcp::model::{
    CallToolRequest, CallToolRequestParams, CancelledNotificationParam, ClientRequest, ServerResult,
};
use rmcp::service::{Peer, PeerRequestOptions, RoleClient, ServiceError};
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

use crate::codes;
use crate::result::tool_result;
use crate::server::ServerStatus;

/// Longest argument summary in [`Tool::summarize`], in characters.
const SUMMARY_CHARS: usize = 80;

/// The registry name of MCP tool `tool` on server `server`.
///
/// It is `mcp__<server>__<tool>` with every character of `tool` outside
/// `[A-Za-z0-9_-]` replaced by `_` (MCP allows `.` in names; providers do
/// not). The registry rejects a result longer than 64 characters.
pub fn tool_name(server: &str, tool: &str) -> String {
    let tool: String = tool
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    format!("mcp__{server}__{tool}")
}

/// One live connection. Replaced, never mutated, on reconnect.
#[derive(Clone)]
pub(crate) struct Conn {
    pub(crate) peer: Peer<RoleClient>,
    /// Cancelled when a call sees this connection's transport fail, so the
    /// supervisor reconnects. A stale connection's token cannot touch the
    /// next connection.
    pub(crate) lost: CancellationToken,
}

/// What every tool of one server shares with the server's supervisor.
pub(crate) struct Link {
    pub(crate) server: String,
    pub(crate) call_timeout: Duration,
    /// `None` while the server is reconnecting, unavailable or closed.
    pub(crate) conn: watch::Sender<Option<Conn>>,
    pub(crate) status: watch::Sender<ServerStatus>,
}

impl Link {
    fn unavailable(&self) -> ToolOutputError {
        let server = &self.server;
        let message = match *self.status.borrow() {
            ServerStatus::Reconnecting => {
                format!("MCP server {server:?} is reconnecting; try again shortly")
            }
            ServerStatus::Unavailable => {
                format!("MCP server {server:?} is unavailable: reconnecting failed")
            }
            ServerStatus::Connected | ServerStatus::Closed => {
                format!("MCP server {server:?} is disconnected")
            }
        };
        coded(message, codes::UNAVAILABLE)
    }

    fn call_failed(&self, conn: &Conn, error: ServiceError) -> ToolOutputError {
        let server = &self.server;
        match error {
            ServiceError::TransportSend(_) | ServiceError::TransportClosed => {
                conn.lost.cancel();
                coded(
                    format!("MCP server {server:?} connection lost during the call: {error}"),
                    codes::UNAVAILABLE,
                )
            }
            ServiceError::Timeout { timeout } => coded(
                format!(
                    "MCP server {server:?} did not answer within {} s; the call was cancelled",
                    timeout.as_secs_f64()
                ),
                codes::TIMEOUT,
            ),
            ServiceError::McpError(error) => coded(
                format!(
                    "MCP server {server:?} rejected the call: {} (code {})",
                    error.message, error.code.0
                ),
                codes::PROTOCOL_ERROR,
            ),
            error => coded(
                format!("MCP call to server {server:?} failed: {error}"),
                codes::PROTOCOL_ERROR,
            ),
        }
    }
}

/// What the model sees of one MCP tool. Compared on relist to decide
/// whether the registration must be replaced.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ToolSpec {
    /// Registry name, `mcp__<server>__<tool>`.
    pub(crate) name: String,
    /// The tool's name on the server.
    pub(crate) remote: String,
    pub(crate) description: String,
    pub(crate) input_schema: JsonObject,
    pub(crate) scope: ToolScope,
}

impl ToolSpec {
    /// The spec for one listed tool. A missing `type` or `properties` in the
    /// input schema defaults to an empty object schema.
    pub(crate) fn new(server: &str, tool: &rmcp::model::Tool, trust_annotations: bool) -> Self {
        let mut input_schema = tool.input_schema.as_ref().clone();
        input_schema
            .entry("type")
            .or_insert_with(|| JsonValue::from("object"));
        input_schema
            .entry("properties")
            .or_insert_with(|| JsonValue::Object(JsonObject::new()));
        let read_only = trust_annotations
            && tool
                .annotations
                .as_ref()
                .and_then(|annotations| annotations.read_only_hint)
                == Some(true);
        Self {
            name: tool_name(server, &tool.name),
            remote: tool.name.to_string(),
            description: tool.description.as_deref().unwrap_or_default().to_owned(),
            input_schema,
            scope: if read_only {
                ToolScope::Read
            } else {
                ToolScope::Exec
            },
        }
    }
}

/// A tool that forwards each call to its server as `tools/call`.
pub(crate) struct McpTool {
    spec: ToolSpec,
    link: Arc<Link>,
}

impl McpTool {
    pub(crate) fn new(spec: ToolSpec, link: Arc<Link>) -> Self {
        Self { spec, link }
    }
}

#[async_trait]
impl Tool for McpTool {
    fn name(&self) -> &str {
        &self.spec.name
    }

    fn description(&self) -> &str {
        &self.spec.description
    }

    fn input_schema(&self) -> &JsonObject {
        &self.spec.input_schema
    }

    fn scope(&self) -> ToolScope {
        self.spec.scope
    }

    fn summarize(&self, input: &JsonObject) -> String {
        if input.is_empty() {
            return self.spec.name.clone();
        }
        let args = JsonValue::Object(input.clone()).to_string();
        let mut short: String = args.chars().take(SUMMARY_CHARS).collect();
        if short.len() < args.len() {
            short.push_str("...");
        }
        format!("{}({short})", self.spec.name)
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        if ctx.cancel.is_cancelled() {
            return Err(coded("[aborted before start]", codes::ABORTED));
        }
        let Some(conn) = self.link.conn.borrow().clone() else {
            return Err(self.link.unavailable());
        };
        let params = CallToolRequestParams::new(self.spec.remote.clone()).with_arguments(input);
        let request = ClientRequest::CallToolRequest(CallToolRequest::new(params));
        let options = PeerRequestOptions::with_timeout(self.link.call_timeout);
        let handle = match conn.peer.send_request_with_option(request, options).await {
            Ok(handle) => handle,
            Err(error) => return Err(self.link.call_failed(&conn, error)),
        };
        let id = handle.id.clone();
        tokio::select! {
            biased;
            () = ctx.cancel.cancelled() => {
                // Over stdio this sends `notifications/cancelled`; over modern
                // streamable HTTP rmcp closes the request's stream instead.
                // Sent in the background: a server that stopped reading its
                // stdin must not keep the cancelled call from returning.
                let reason = Some("cancelled by the caller".to_owned());
                let notice = CancelledNotificationParam::new(Some(id), reason);
                let peer = conn.peer.clone();
                tokio::spawn(async move {
                    let _ = peer.notify_cancelled(notice).await;
                });
                Err(coded("[aborted; the MCP request was cancelled]", codes::ABORTED))
            }
            response = handle.await_response() => match response {
                Ok(ServerResult::CallToolResult(result)) => tool_result(result),
                Ok(_) => Err(coded(
                    format!(
                        "MCP server {:?} asked for input or started a task; \
                         this client supports neither",
                        self.link.server
                    ),
                    codes::PROTOCOL_ERROR,
                )),
                Err(error) => Err(self.link.call_failed(&conn, error)),
            },
        }
    }
}

fn coded(message: impl Into<String>, code: &str) -> ToolOutputError {
    ToolOutputError {
        message: message.into(),
        code: Some(code.to_owned()),
        details: None,
    }
}

#[cfg(test)]
mod tests {
    use rmcp::model::ToolAnnotations;
    use serde_json::json;

    use super::*;

    fn listed(name: &str, schema: JsonValue, read_only: Option<bool>) -> rmcp::model::Tool {
        let JsonValue::Object(schema) = schema else {
            panic!("schema must be an object")
        };
        let mut tool = rmcp::model::Tool::new(name.to_owned(), "Does a thing.", Arc::new(schema));
        tool.annotations = read_only.map(|hint| {
            let mut annotations = ToolAnnotations::new();
            annotations.read_only_hint = Some(hint);
            annotations
        });
        tool
    }

    #[test]
    fn names_are_prefixed_and_normalized() {
        assert_eq!(tool_name("fs", "read_file"), "mcp__fs__read_file");
        assert_eq!(
            tool_name("gh", "admin.tools.list"),
            "mcp__gh__admin_tools_list"
        );
        assert_eq!(tool_name("gh", "a b/c"), "mcp__gh__a_b_c");
    }

    #[test]
    fn scope_is_exec_unless_a_trusted_read_only_hint() {
        let tool = listed("t", json!({"type": "object"}), Some(true));
        assert_eq!(ToolSpec::new("s", &tool, false).scope, ToolScope::Exec);
        assert_eq!(ToolSpec::new("s", &tool, true).scope, ToolScope::Read);

        let writer = listed("t", json!({"type": "object"}), Some(false));
        assert_eq!(ToolSpec::new("s", &writer, true).scope, ToolScope::Exec);
        let plain = listed("t", json!({"type": "object"}), None);
        assert_eq!(ToolSpec::new("s", &plain, true).scope, ToolScope::Exec);
    }

    #[test]
    fn the_input_schema_is_the_servers_with_object_defaults() {
        let schema = json!({
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        });
        let spec = ToolSpec::new("s", &listed("t", schema.clone(), None), false);
        assert_eq!(JsonValue::Object(spec.input_schema), schema);
        assert_eq!(spec.description, "Does a thing.");
        assert_eq!(spec.remote, "t");

        let bare = ToolSpec::new("s", &listed("t", json!({}), None), false);
        assert_eq!(
            JsonValue::Object(bare.input_schema),
            json!({"type": "object", "properties": {}})
        );
    }
}
