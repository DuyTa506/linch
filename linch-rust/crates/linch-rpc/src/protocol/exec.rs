//! Server requests that run a `remote` execution backend's commands in the
//! client: `exec.start`, `exec.read` and `exec.kill`.

use std::collections::BTreeMap;

use linch::{Principal, SessionId};
use serde::{Deserialize, Serialize};

/// Params of `exec.start`: start one command. The result is `{}`; an error
/// object fails the call the model made (`spawn_failed`, with the error's
/// message).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecStartParams {
    /// Agent handle the backend belongs to.
    pub agent: String,
    /// Names the command in `exec.read` and `exec.kill`; unique per agent.
    pub exec_id: String,
    /// The command, for `/bin/sh -c`.
    pub command: String,
    /// Where it starts; absent: the host's choice.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    /// The variables the command gets: the server's allowlisted ones
    /// without its `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR`, which the
    /// host supplies itself.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub env: BTreeMap<String, String>,
    /// Whether it is a background command: the model does not wait for it.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub background: bool,
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Who the run that made the call acts for: the `principal` of its
    /// `run.start`. Absent when the run has none. Several principals' runs
    /// on one agent share the client's host, so a host that serves more than
    /// one scopes or denies the command by it; the connection's own tenant
    /// is the only one a tenant's principal can name.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `exec.read`: wait up to `wait_ms` for output or the end of a
/// command, and take what is new. The host answers at once when it has
/// something. The result is a `RemoteChunk`,
/// `{"stdout"?: "", "stderr"?: "", "exit"?: {"code": 0} | {"signal": 9}}`;
/// `exit` comes once, after the output it follows, and the host forgets the
/// command then.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecReadParams {
    /// Agent handle the backend belongs to.
    pub agent: String,
    /// The command to read.
    pub exec_id: String,
    /// Longest wait for something new, in milliseconds.
    pub wait_ms: u64,
}

/// Params of `exec.kill`: kill the command and everything it started, and
/// answer when it is dead. The result is a `RemoteChunk` with what it
/// printed since the last read and its `exit`; the host forgets the
/// command. Answering is the confirmation: without it the server reports
/// the call as interrupted, not as killed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecKillParams {
    /// Agent handle the backend belongs to.
    pub agent: String,
    /// The command to kill.
    pub exec_id: String,
}
