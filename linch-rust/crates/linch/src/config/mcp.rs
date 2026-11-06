//! [`McpConfig`]: one MCP server of an [`AgentConfig`](super::AgentConfig),
//! the serde mirror of [`McpServerConfig`].

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use linch_mcp::{McpServerConfig, McpTransport, ReconnectPolicy};
use serde::{Deserialize, Serialize};

use super::redact;

/// One MCP server. The agent connects it when it opens, registers its tools
/// as `mcp__<name>__<tool>`, and disconnects it when it closes.
///
/// Durations are in milliseconds. Zero timeouts are refused when the agent
/// opens.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct McpConfig {
    /// Server name, the `<server>` in `mcp__<server>__<tool>`: ASCII
    /// letters, digits, `-`, and single `_` between them.
    pub name: String,
    /// How to reach the server.
    pub transport: McpTransportConfig,
    /// Whether to believe the server's `readOnlyHint`: a read-only tool then
    /// gets the `read` scope instead of `exec`. Default `false`.
    #[serde(default)]
    pub trust_annotations: bool,
    /// Longest wait for one tool call before it is cancelled (default
    /// 300 000).
    #[serde(default = "default_call_timeout_ms")]
    pub call_timeout_ms: u64,
    /// How a lost connection is retried.
    #[serde(default)]
    pub reconnect: ReconnectConfig,
}

/// How to reach one MCP server, by `type`.
///
/// `Debug` and serializing show environment and header names but not their
/// values, which often hold secrets, nor the stdio arguments or a URL's
/// user name, password and query: each is written as `[redacted]` (inside
/// a URL, `redacted`).
/// [`AgentConfig::to_value_with_secrets`](super::AgentConfig::to_value_with_secrets)
/// keeps them.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum McpTransportConfig {
    /// A child process speaking JSON-RPC on its stdin and stdout.
    Stdio {
        /// Program to run, looked up on `PATH`.
        command: String,
        /// Arguments passed to the program.
        #[serde(default, serialize_with = "super::redact::args")]
        args: Vec<String>,
        /// Extra environment variables. Of the server's own environment the
        /// child sees only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and
        /// `USER`.
        #[serde(default, serialize_with = "super::redact::values")]
        env: BTreeMap<String, String>,
    },
    /// A streamable HTTP endpoint.
    Http {
        /// Endpoint URL, for example `http://127.0.0.1:8000/mcp`.
        #[serde(serialize_with = "super::redact::url")]
        url: String,
        /// Headers sent with every request, for example `Authorization`.
        #[serde(default, serialize_with = "super::redact::values")]
        headers: BTreeMap<String, String>,
    },
}

/// How a lost MCP connection is retried; every field defaults to
/// [`ReconnectPolicy::default`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ReconnectConfig {
    /// Reconnect attempts after a loss; 0 never reconnects.
    pub attempts: u32,
    /// Wait before the first attempt.
    pub initial_delay_ms: u64,
    /// Longest wait between attempts.
    pub max_delay_ms: u64,
    /// Time one attempt may take, the first connect included.
    pub attempt_timeout_ms: u64,
}

fn default_call_timeout_ms() -> u64 {
    300_000
}

fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

impl Default for ReconnectConfig {
    fn default() -> Self {
        let policy = ReconnectPolicy::default();
        Self {
            attempts: policy.attempts,
            initial_delay_ms: millis(policy.initial_delay),
            max_delay_ms: millis(policy.max_delay),
            attempt_timeout_ms: millis(policy.attempt_timeout),
        }
    }
}

impl From<McpConfig> for McpServerConfig {
    fn from(config: McpConfig) -> Self {
        let transport = match config.transport {
            McpTransportConfig::Stdio { command, args, env } => {
                McpTransport::Stdio { command, args, env }
            }
            McpTransportConfig::Http { url, headers } => McpTransport::Http { url, headers },
        };
        let reconnect = config.reconnect;
        let mut server = Self::new(config.name, transport);
        server.trust_annotations = config.trust_annotations;
        server.call_timeout = Duration::from_millis(config.call_timeout_ms);
        server.reconnect = ReconnectPolicy {
            attempts: reconnect.attempts,
            initial_delay: Duration::from_millis(reconnect.initial_delay_ms),
            max_delay: Duration::from_millis(reconnect.max_delay_ms),
            attempt_timeout: Duration::from_millis(reconnect.attempt_timeout_ms),
        };
        server
    }
}

impl fmt::Debug for McpTransportConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Stdio { command, args, env } => f
                .debug_struct("Stdio")
                .field("command", command)
                .field("args", &vec![redact::REDACTED; args.len()])
                .field("env", &env.keys().collect::<Vec<_>>())
                .finish(),
            Self::Http { url, headers } => f
                .debug_struct("Http")
                .field("url", &redact::redact_url(url))
                .field("headers", &headers.keys().collect::<Vec<_>>())
                .finish(),
        }
    }
}
