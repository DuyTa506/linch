//! MCP client for the Linch agent kernel.
//!
//! [`McpServer::connect`] reaches one MCP server, a stdio child process or a
//! streamable HTTP endpoint, lists its tools and registers each in a
//! [`ToolRegistry`](linch_core::ToolRegistry) under [`tool_name`]
//! (`mcp__<server>__<tool>`):
//!
//! - the scope is `exec`, or `read` for a `readOnlyHint` tool when the
//!   config sets `trust_annotations`;
//! - the input schema is the server's `inputSchema`;
//! - text content becomes the output, an `isError` result a
//!   [`ToolOutputError`](linch_core::ToolOutputError), and images are kept in
//!   `metadata.images`.
//!
//! The server stays connected. On `notifications/tools/list_changed`, and
//! after every reconnect, it lists the tools again and replaces only what
//! changed. A lost connection is retried per [`ReconnectPolicy`]; a call made
//! meanwhile gets a tool error the model can read. When
//! `ToolContext::cancel` fires, the server is sent MCP cancellation for the
//! request. [`McpServer::disconnect`] disposes every registration it made.
//!
//! Built on [`rmcp`], the official Rust SDK, speaking protocol 2026-07-28
//! with a fallback to the 2025-11-25 `initialize` handshake. Legacy SSE,
//! resources, prompts, sampling, elicitation and OAuth are not supported.

mod catalog;
pub mod codes;
mod config;
mod connector;
mod result;
mod server;
mod tool;

pub use catalog::SkippedTool;
pub use config::{ConfigError, McpServerConfig, McpTransport, ReconnectPolicy};
pub use connector::{ConnectError, Connector, Handler};
pub use rmcp;
pub use server::{McpError, McpServer, ServerStatus};
pub use tool::tool_name;
