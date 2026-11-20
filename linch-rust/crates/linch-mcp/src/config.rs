//! Server configuration: transport, timeouts and the reconnect policy.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use thiserror::Error;

use crate::connector::Connector;

/// Why a server configuration was rejected.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ConfigError {
    /// The server name is not ASCII letters, digits, `-` and single `_`
    /// between them. A `__` or an edge `_` would make `mcp__<server>__<tool>`
    /// ambiguous.
    #[error(
        "MCP server name {name:?} must be ASCII letters, digits, '-' or single '_' between them"
    )]
    InvalidServerName {
        /// The rejected name.
        name: String,
    },
    /// An HTTP header name or value is not valid HTTP.
    #[error("MCP server header {name:?} has an invalid name or value")]
    InvalidHeader {
        /// The header name as configured.
        name: String,
    },
    /// A timeout is zero, so every call or attempt would fail at once.
    #[error("MCP server {which} timeout must be greater than zero")]
    ZeroTimeout {
        /// Which timeout: `call` or `attempt`.
        which: &'static str,
    },
}

/// How to reach one MCP server.
#[derive(Clone)]
pub enum McpTransport {
    /// Start a child process and speak JSON-RPC over its stdin and stdout.
    Stdio {
        /// Program to run, looked up on `PATH`.
        command: String,
        /// Arguments passed to the program.
        args: Vec<String>,
        /// Extra environment variables. Of this process's environment the
        /// child sees only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and
        /// `USER`, so host secrets do not leak to a server.
        env: BTreeMap<String, String>,
    },
    /// A streamable HTTP endpoint.
    Http {
        /// Endpoint URL, for example `http://127.0.0.1:8000/mcp`.
        url: String,
        /// Headers sent with every request, for example `Authorization`.
        headers: BTreeMap<String, String>,
    },
    /// A caller-supplied connector, for in-process servers and tests.
    Custom(Arc<dyn Connector>),
}

impl McpTransport {
    /// A stdio server started as `command args...`, with no extra environment.
    pub fn stdio(command: impl Into<String>, args: impl IntoIterator<Item = String>) -> Self {
        Self::Stdio {
            command: command.into(),
            args: args.into_iter().collect(),
            env: BTreeMap::new(),
        }
    }

    /// A streamable HTTP server at `url`, with no extra headers.
    pub fn http(url: impl Into<String>) -> Self {
        Self::Http {
            url: url.into(),
            headers: BTreeMap::new(),
        }
    }
}

/// `url` as `Debug` shows it: the user name, password and query replaced and
/// the fragment dropped, since a token often sits in them. A string that is
/// not a URL, or has no host (`user:token@host:8443/mcp` parses as scheme
/// `user`), is replaced whole. The facade's `redact::redact_url` does the
/// same for an `AgentConfig` (`linch` depends on this crate, so the small
/// function is repeated here).
fn redacted_url(url: &str) -> String {
    const REDACTED: &str = "redacted";
    let Ok(mut parsed) = reqwest::Url::parse(url) else {
        return "[redacted]".to_owned();
    };
    if parsed.cannot_be_a_base() || parsed.host_str().is_none() {
        return "[redacted]".to_owned();
    }
    if parsed.username().is_empty()
        && parsed.password().is_none()
        && parsed.query().is_none()
        && parsed.fragment().is_none()
    {
        return url.to_owned();
    }
    parsed.set_fragment(None);
    let user = parsed.username().is_empty() || parsed.set_username(REDACTED).is_ok();
    let password = parsed.password().is_none() || parsed.set_password(Some(REDACTED)).is_ok();
    if !(user && password) {
        return "[redacted]".to_owned();
    }
    if parsed.query().is_some() {
        parsed.set_query(Some(REDACTED));
    }
    parsed.into()
}

/// Environment and header values are left out: they often hold secrets. A
/// URL's user name, password and query are replaced and its fragment dropped.
impl fmt::Debug for McpTransport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Stdio { command, args, env } => f
                .debug_struct("Stdio")
                .field("command", command)
                .field("args", args)
                .field("env", &env.keys().collect::<Vec<_>>())
                .finish(),
            Self::Http { url, headers } => f
                .debug_struct("Http")
                .field("url", &redacted_url(url))
                .field("headers", &headers.keys().collect::<Vec<_>>())
                .finish(),
            Self::Custom(_) => f.write_str("Custom(..)"),
        }
    }
}

/// How a lost connection is retried.
///
/// Attempt `n` (from 0) waits `min(max_delay, initial_delay * 2^n)` first.
/// After `attempts` failures the server is unavailable until it is
/// disconnected; its tools stay registered and return a tool error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconnectPolicy {
    /// Reconnect attempts after a loss; 0 never reconnects.
    pub attempts: u32,
    /// Wait before the first attempt.
    pub initial_delay: Duration,
    /// Longest wait between attempts.
    pub max_delay: Duration,
    /// Time one attempt may take: connect, subscribe and list the tools. The
    /// first connect uses it too.
    pub attempt_timeout: Duration,
}

impl ReconnectPolicy {
    /// Wait before attempt `attempt` (from 0).
    pub fn delay(&self, attempt: u32) -> Duration {
        let factor = 2u32.saturating_pow(attempt);
        self.initial_delay
            .saturating_mul(factor)
            .min(self.max_delay)
    }
}

impl Default for ReconnectPolicy {
    /// Five attempts, 0.5 s doubling to at most 30 s, 30 s per attempt.
    fn default() -> Self {
        Self {
            attempts: 5,
            initial_delay: Duration::from_millis(500),
            max_delay: Duration::from_secs(30),
            attempt_timeout: Duration::from_secs(30),
        }
    }
}

/// One MCP server and how its tools are exposed.
#[derive(Debug, Clone)]
pub struct McpServerConfig {
    /// Server name, the `<server>` in `mcp__<server>__<tool>`.
    pub name: String,
    /// How to reach the server.
    pub transport: McpTransport,
    /// Whether to believe the server's `readOnlyHint`: a read-only tool gets
    /// the `read` scope (auto-approved, may run in parallel) instead of `exec`.
    pub trust_annotations: bool,
    /// The server's tools (by the names it lists them under) that poll: they
    /// ask after something that runs on, so the loop guard waits on their
    /// repeated answers instead of counting them as a loop (see
    /// [`linch_core::Tool::polls`]). An MCP result has no metadata to say the
    /// wait is over, so a poll that never changes ends the run at the
    /// guard's `max_idle_turns`.
    pub polls: BTreeSet<String>,
    /// Longest wait for one tool call before it is cancelled.
    pub call_timeout: Duration,
    /// How a lost connection is retried.
    pub reconnect: ReconnectPolicy,
}

impl McpServerConfig {
    /// A server with untrusted annotations, a 300 s call timeout and the
    /// default reconnect policy.
    pub fn new(name: impl Into<String>, transport: McpTransport) -> Self {
        Self {
            name: name.into(),
            transport,
            trust_annotations: false,
            polls: BTreeSet::new(),
            call_timeout: Duration::from_secs(300),
            reconnect: ReconnectPolicy::default(),
        }
    }

    /// Check the name and timeouts. Headers are checked when the transport
    /// is built.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !is_valid_server_name(&self.name) {
            return Err(ConfigError::InvalidServerName {
                name: self.name.clone(),
            });
        }
        if self.call_timeout.is_zero() {
            return Err(ConfigError::ZeroTimeout { which: "call" });
        }
        if self.reconnect.attempt_timeout.is_zero() {
            return Err(ConfigError::ZeroTimeout { which: "attempt" });
        }
        Ok(())
    }
}

/// Non-empty `[A-Za-z0-9_-]`, with no `__` and no `_` at either end.
fn is_valid_server_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
        && !name.contains("__")
        && !name.starts_with('_')
        && !name.ends_with('_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_names_must_split_cleanly() {
        for good in ["fs", "git-hub", "my_server", "a1"] {
            assert!(is_valid_server_name(good), "{good}");
        }
        for bad in ["", "a__b", "_a", "a_", "a.b", "a b", "é"] {
            assert!(!is_valid_server_name(bad), "{bad}");
        }
    }

    #[test]
    fn validate_rejects_names_and_zero_timeouts() {
        let config = McpServerConfig::new("fs", McpTransport::http("http://127.0.0.1:1/mcp"));
        assert_eq!(config.validate(), Ok(()));

        let mut bad = config.clone();
        bad.name = "a__b".into();
        assert!(matches!(
            bad.validate(),
            Err(ConfigError::InvalidServerName { .. })
        ));

        let mut bad = config.clone();
        bad.call_timeout = Duration::ZERO;
        assert_eq!(
            bad.validate(),
            Err(ConfigError::ZeroTimeout { which: "call" })
        );

        let mut bad = config;
        bad.reconnect.attempt_timeout = Duration::ZERO;
        assert_eq!(
            bad.validate(),
            Err(ConfigError::ZeroTimeout { which: "attempt" })
        );
    }

    #[test]
    fn backoff_doubles_up_to_the_cap() {
        let policy = ReconnectPolicy::default();
        let delays: Vec<_> = (0..8).map(|n| policy.delay(n).as_millis()).collect();
        assert_eq!(delays, [500, 1000, 2000, 4000, 8000, 16000, 30000, 30000]);
        assert_eq!(policy.delay(u32::MAX), policy.max_delay);
    }

    #[test]
    fn debug_hides_the_userinfo_and_query_of_a_url() {
        let http = McpTransport::Http {
            url: "https://user:tok-pass@mcp.internal:8443/mcp?key=tok-query".into(),
            headers: BTreeMap::new(),
        };
        let shown = format!("{http:?}");
        assert!(
            !shown.contains("tok-pass") && !shown.contains("tok-query"),
            "{shown}"
        );
        assert!(!shown.contains("user:"), "{shown}");
        assert!(shown.contains("mcp.internal:8443/mcp"), "{shown}");
        let plain = McpTransport::http("http://127.0.0.1:8000/mcp");
        assert!(format!("{plain:?}").contains("http://127.0.0.1:8000/mcp"));
        let not_a_url = McpTransport::http("not a url?tok-query");
        assert!(!format!("{not_a_url:?}").contains("tok-query"));
    }

    /// Where a token can sit in a URL that `Debug` prints: the query, the
    /// userinfo, escapes, the fragment, and a URL written without its
    /// scheme (it parses as scheme `user`, with no host).
    #[test]
    fn debug_of_a_url_matrix() {
        let mut leaks = Vec::new();
        for url in [
            "https://mcp.internal/mcp?tok-secret",
            "https://tok-secret@mcp.internal/mcp",
            "https://user:tok-secret@[::1]:8443/mcp",
            "https://u:tok%2Dsecret@mcp.internal/mcp?k=tok%2Dsecret",
            "ws://u:tok-secret@mcp.internal/mcp",
            "unix://u:tok-secret@host/mcp",
            "HTTPS://U:tok-secret@MCP.internal/mcp",
            " https://u:tok-secret@mcp.internal/mcp",
            "https://mcp.internal/mcp#access_token=tok-secret",
            "user:tok-secret@mcp.internal:8443/mcp",
            "mcp.internal:8443/mcp?key=tok-secret",
            "//u:tok-secret@mcp.internal/mcp",
        ] {
            let shown = format!("{:?}", McpTransport::http(url));
            if shown.contains("tok-secret") || shown.contains("tok%2Dsecret") {
                leaks.push(shown);
            }
        }
        for url in [
            "http://127.0.0.1:8000/mcp",
            "http://[::1]:8000/mcp/",
            "http://GW.internal:80/a%20b",
        ] {
            let shown = format!("{:?}", McpTransport::http(url));
            assert!(shown.contains(url), "{url}: {shown}");
        }
        assert!(leaks.is_empty(), "{leaks:#?}");
    }

    #[test]
    fn debug_hides_secret_values() {
        let mut headers = BTreeMap::new();
        headers.insert("Authorization".into(), "Bearer secret".into());
        let http = McpTransport::Http {
            url: "https://example.com/mcp".into(),
            headers,
        };
        let shown = format!("{http:?}");
        assert!(shown.contains("Authorization"));
        assert!(!shown.contains("secret"));

        let mut env = BTreeMap::new();
        env.insert("TOKEN".into(), "hunter2".into());
        let stdio = McpTransport::Stdio {
            command: "server".into(),
            args: vec![],
            env,
        };
        assert!(!format!("{stdio:?}").contains("hunter2"));
    }
}
