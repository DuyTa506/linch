//! `server.info`: what a client learns about the server at connect.
//!
//! Every member is a fact of the build, so every caller, a local client and
//! a tenant alike, gets the same answer: it holds no tenant's caps, names,
//! hosts or paths.

use serde::{Deserialize, Serialize};

use crate::server::MAX_LINE_BYTES;

/// The wire protocol's version. Bump it only for a change an older client
/// would misread (a removed or retyped member, a changed meaning); an added
/// method, member or capability keeps it. A client names the range it
/// speaks and refuses a server outside it.
pub const PROTOCOL_VERSION: u32 = 1;

/// Optional features a client may rely on, by name. A name is never reused
/// for another behavior, and a client ignores names it does not know.
pub const CAPABILITIES: &[&str] = &[
    "cache_isolation",
    "exec_idempotency_key",
    "provider_info",
    "run_events_paging",
    "run_steer",
    "schedule",
    "workflow",
    "workflow_step_abandon",
];

/// The execution backends this build implements, in config order. Whether
/// one works here (a Docker daemon, Landlock in the kernel) or is allowed to
/// a tenant is decided when an agent opens.
#[cfg(target_os = "linux")]
pub const BACKENDS: &[&str] = &["local", "sandbox", "docker", "remote"];
/// The execution backends this build implements; Landlock and seccomp are
/// Linux's.
#[cfg(not(target_os = "linux"))]
pub const BACKENDS: &[&str] = &["local", "docker", "remote"];

/// Longest `client_version` the server accepts.
const MAX_CLIENT_VERSION_BYTES: usize = 64;

/// Params of `server.info`. Both members are optional and only logged: the
/// first call is made before the client knows the server, so its shape must
/// never need to change.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InfoParams {
    /// The client's own version, for the server's log.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_version: Option<String>,
    /// The newest protocol version the client speaks.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_protocol: Option<u32>,
}

impl InfoParams {
    /// Why these params are refused, or `None`.
    pub(crate) fn problem(&self) -> Option<String> {
        let version = self.client_version.as_deref()?;
        (version.len() > MAX_CLIENT_VERSION_BYTES || version.chars().any(char::is_control))
            .then(|| format!("client_version is at most {MAX_CLIENT_VERSION_BYTES} bytes of text"))
    }
}

/// The main limits of the server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InfoLimits {
    /// Longest message the server reads, in bytes; a longer one is refused
    /// with `-32600`.
    pub max_message_bytes: u64,
    /// Runs a tenant may drive at once unless its allowlists say otherwise
    /// (a local client has no cap).
    pub default_max_runs: u64,
}

/// Result of `server.info`.
///
/// Unlike the other results it accepts members it does not know, so a
/// client of this type still reads a later server's answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerInfo {
    /// The server's release, as in its package.
    pub server_version: String,
    /// The wire protocol's version ([`PROTOCOL_VERSION`]).
    pub protocol: u32,
    /// The run contract digest's version ([`linch::CONTRACT_VERSION`]): runs
    /// stored under another one do not resume.
    pub contract_version: u32,
    /// Names of the optional features this server has ([`CAPABILITIES`]).
    pub capabilities: Vec<String>,
    /// The execution backends it implements ([`BACKENDS`]).
    pub backends: Vec<String>,
    /// Its main limits.
    pub limits: InfoLimits,
}

impl ServerInfo {
    /// The answer of this build.
    pub fn current() -> Self {
        let names = |names: &[&str]| names.iter().map(|name| (*name).to_owned()).collect();
        Self {
            server_version: env!("CARGO_PKG_VERSION").to_owned(),
            protocol: PROTOCOL_VERSION,
            contract_version: linch::CONTRACT_VERSION,
            capabilities: names(CAPABILITIES),
            backends: names(BACKENDS),
            limits: InfoLimits {
                max_message_bytes: MAX_LINE_BYTES as u64,
                default_max_runs: crate::access::DEFAULT_MAX_RUNS as u64,
            },
        }
    }
}
