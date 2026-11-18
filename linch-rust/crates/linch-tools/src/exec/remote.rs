//! The `remote` backend: `Bash` commands run on a host the agent's client
//! supplies, over a connection the embedder owns.
//!
//! The crate knows no transport. A host implements [`RemoteExec`]: start a
//! command, read what it printed (a long poll), kill it. `linch-rpc`
//! implements it over the client's connection, with `exec.start`,
//! `exec.read` and `exec.kill`; a test implements it in memory.
//!
//! The server only ever reports what the host told it. A command's exit
//! comes from a read (or a confirmed kill: an answer with an exit). If the
//! host stops answering, its connection closes, it does not confirm a kill,
//! or a start's answer is lost, the call ends `End::Interrupted`: the
//! command's outcome is unknown, and no exit code is made up. Only a start
//! the host refuses outright proves the command never ran.

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{Principal, SessionId};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::time::{Instant, sleep_until, timeout};
use tokio_util::sync::CancellationToken;

use super::{Launch, Shell};
use crate::bash::DEADLINE_MARGIN;
use crate::bash::process::{End, Started};

/// How long one read waits on the host for new output before it answers
/// empty. The server polls with this, so a silent command costs one round
/// trip per interval.
pub const READ_WAIT: Duration = Duration::from_secs(1);

/// How long past [`READ_WAIT`] the host may take to answer a read before it
/// counts as unresponsive.
const READ_GRACE: Duration = Duration::from_secs(5);

/// How long the host has to confirm a kill, and to accept a start.
pub const HOST_ANSWER: Duration = Duration::from_secs(5);

/// The fewest time between the starts of two reads when the host answered
/// the first at once with next to nothing, so a host that never waits cannot
/// make the server send one request after another.
const MIN_POLL: Duration = Duration::from_millis(25);

/// An answer shorter than this (both streams together) is next to nothing.
const SMALL_ANSWER: usize = 4096;

/// The most output of one command the server takes before it kills the
/// command: the model keeps 30 KB of it, so a host that never stops is not
/// a command that is still printing.
const OUTPUT_BUDGET: usize = 256 * 1024 * 1024;

/// The longest host error text shown to the model, in bytes.
const REFUSAL_LIMIT: usize = 2048;

/// How long before a call's deadline a remote command must end: a kill
/// (up to [`HOST_ANSWER`]), a read that may be in flight and the result.
const KILL_MARGIN: Duration = HOST_ANSWER
    .saturating_add(READ_WAIT)
    .saturating_add(DEADLINE_MARGIN);

/// Host variables of `Bash`'s environment that describe the server, not the
/// command: a remote host builds its own.
const SERVER_ONLY: [&str; 5] = ["PATH", "HOME", "USER", "SHELL", "TMPDIR"];

/// How a command ended, as a host reports it.
///
/// On the wire: `{"code": 0}` or `{"signal": 9}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Exit {
    /// The shell exited with this status.
    Code(i32),
    /// The shell was killed by this signal.
    Signal(i32),
}

/// What a command printed since the last read, and how it ended once it
/// has.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteChunk {
    /// New standard output, decoded as UTF-8 by the host.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub stdout: String,
    /// New standard error, decoded as UTF-8 by the host.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub stderr: String,
    /// Present exactly once, in the last chunk, when the command ended;
    /// the host forgets the command after sending it. A kill answer without
    /// it does not confirm the command is dead.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit: Option<Exit>,
}

/// One command for the host to start.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteCommand {
    /// Names the command in later reads and kills; unique per agent.
    pub id: String,
    /// The shell command, for `/bin/sh -c`.
    pub command: String,
    /// Where it starts: the policy's `cwd`, or the server's workspace path
    /// when the policy shares the workspace; `None` is the host's choice.
    pub cwd: Option<String>,
    /// The variables the command gets, without the server's `PATH`, `HOME`,
    /// `USER`, `SHELL` and `TMPDIR`: the host adds its own.
    pub env: BTreeMap<String, String>,
    /// Whether it is a background command (`run_in_background`).
    pub background: bool,
    /// The session the call belongs to.
    pub session: SessionId,
    /// Who the run acts for, when the host that started it named one. A host
    /// that serves several principals scopes or denies the command by it.
    pub principal: Option<Principal>,
}

/// Why the host did not do what the server asked.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RemoteError {
    /// The host answered with an error; this is its message. For a start,
    /// the only error that proves the command did not run.
    #[error("{0}")]
    Rejected(String),
    /// The connection to the host is closed.
    #[error("the host connection is closed")]
    Closed,
    /// The host did not answer in time.
    #[error("the host did not answer in time")]
    Unresponsive,
    /// The host's answer was not what the protocol says.
    #[error("the host's answer could not be read: {0}")]
    Protocol(String),
}

/// A host that runs an agent's `Bash` commands. One belongs to one agent,
/// over one live connection.
///
/// The server wraps every call in its own timeout and drops the future of
/// a call it gives up on, so an implementation need not time out, but
/// should send a cancel to the host when dropped.
#[async_trait]
pub trait RemoteExec: Send + Sync + fmt::Debug {
    /// Start `command`. Return [`RemoteError::Rejected`] only when the host
    /// did not start it: any other error (or none in time) leaves the
    /// server unsure, so it kills the command and reports the call
    /// interrupted.
    async fn start(&self, command: RemoteCommand) -> Result<(), RemoteError>;

    /// Wait up to `wait` for output or the end of command `id`, and take
    /// what is new. It returns at once when there is something to take.
    async fn read(&self, id: &str, wait: Duration) -> Result<RemoteChunk, RemoteError>;

    /// Kill command `id` and everything it started, wait until it is dead
    /// and take what it printed since the last read, with its exit. An
    /// answer without the exit does not confirm that it is dead.
    async fn kill(&self, id: &str) -> Result<RemoteChunk, RemoteError>;
}

/// The policy of the `remote` backend: `{"backend": "remote", ...}`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemotePolicy {
    /// The directory commands start in on the host; the host's own default
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    /// Whether the host sees the server's workspace at the same path (a
    /// shared mount, or the same machine). Off by default: the file tools
    /// act on the server's workspace, a different place from where the
    /// commands run, so they are not registered. On: they are, and
    /// commands start in the workspace unless `cwd` says otherwise.
    #[serde(default, skip_serializing_if = "is_false")]
    pub shared_workspace: bool,
}

fn is_false(value: &bool) -> bool {
    !value
}

impl RemotePolicy {
    /// The sentence that ends `Bash`'s description. Every field shows, so
    /// every change of policy changes the run contract.
    pub(super) fn describe(&self) -> String {
        let place = match (&self.cwd, self.shared_workspace) {
            (Some(cwd), _) => format!("the directory {cwd}"),
            (None, true) => "the workspace".to_owned(),
            (None, false) => "the host's default directory".to_owned(),
        };
        let files = match self.shared_workspace {
            true => {
                "The host sees the workspace at the same path, so a file a command \
                 writes is the file Read reads."
            }
            false => "There are no file tools: the server's workspace is not the host's.",
        };
        format!(
            "Commands run on a remote host that the client supplies, not on the server, \
             starting in {place}, under the host's own isolation. {files}"
        )
    }
}

/// The shell of the `remote` backend: starts commands through its host.
pub(super) struct RemoteShell {
    host: Arc<dyn RemoteExec>,
    policy: RemotePolicy,
    started: AtomicU64,
}

impl RemoteShell {
    pub(super) fn new(host: Arc<dyn RemoteExec>, policy: RemotePolicy) -> Self {
        Self {
            host,
            policy,
            started: AtomicU64::new(0),
        }
    }
}

impl fmt::Debug for RemoteShell {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RemoteShell")
            .field("policy", &self.policy)
            .finish_non_exhaustive()
    }
}

/// `message` as the model may read it: plain text (control characters
/// become spaces) of at most [`REFUSAL_LIMIT`] bytes.
fn bounded(message: &str) -> String {
    let mut text = String::new();
    for c in message.chars() {
        let c = if c.is_control() { ' ' } else { c };
        if text.len() + c.len_utf8() > REFUSAL_LIMIT {
            text.push_str("...");
            break;
        }
        text.push(c);
    }
    text
}

#[async_trait]
impl Shell for RemoteShell {
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Started> {
        let number = self.started.fetch_add(1, Ordering::Relaxed) + 1;
        let id = format!("exec-{number}");
        let cwd = (self.policy.cwd.clone()).or_else(|| {
            self.policy
                .shared_workspace
                .then(|| launch.cwd.display().to_string())
        });
        let env = (launch.env.iter())
            .filter(|(key, _)| !SERVER_ONLY.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        let command = RemoteCommand {
            id: id.clone(),
            command: launch.command.to_owned(),
            cwd,
            env,
            background: launch.lifeline,
            session: launch.session.clone(),
            principal: launch.principal.cloned(),
        };
        // Dropped while the host starts it: the host may have, so stop it.
        let mut run = RemoteRun::new(Arc::clone(&self.host), id);
        match timeout(HOST_ANSWER, self.host.start(command)).await {
            Ok(Ok(())) => Ok(Started::Remote(run)),
            // Only a refusal proves the host did not start it.
            Ok(Err(RemoteError::Rejected(message))) => {
                run.armed = false;
                Err(io::Error::other(bounded(&message)))
            }
            // Anything else may follow a start that happened: an answer
            // that was lost, unreadable or late. The call must not look
            // like a command that never ran (the model would run it twice),
            // so it is watched and killed, and ends interrupted.
            Ok(Err(_)) | Err(_) => {
                run.unconfirmed = true;
                Ok(Started::Remote(run))
            }
        }
    }

    fn program(&self) -> &str {
        "the remote command"
    }

    fn deadline_margin(&self, left: Duration) -> Duration {
        // Half of a short deadline at most, so it does not shrink to nothing.
        KILL_MARGIN.min(left / 2).max(DEADLINE_MARGIN)
    }
}

/// A command the host runs. Stops it, best effort, when dropped while it
/// may still run.
pub(crate) struct RemoteRun {
    host: Arc<dyn RemoteExec>,
    id: String,
    armed: bool,
    /// The host's answer to the start did not say whether it started.
    unconfirmed: bool,
}

impl fmt::Debug for RemoteRun {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RemoteRun").field("id", &self.id).finish()
    }
}

/// Why the loop stopped before the command ended.
enum Stop {
    TimedOut,
    Aborted,
    /// The host stopped answering.
    Lost,
    /// The command printed more than [`OUTPUT_BUDGET`].
    Flooded,
}

impl RemoteRun {
    fn new(host: Arc<dyn RemoteExec>, id: String) -> Self {
        Self {
            host,
            id,
            armed: true,
            unconfirmed: false,
        }
    }

    /// Poll the host for output until the command ends, `timeout` passes or
    /// `cancel` fires, feeding `on_out` and `on_err`. On a timeout or
    /// cancel the host is told to kill the command; the end is then
    /// [`End::TimedOut`] or [`End::Aborted`] only if the host confirmed the
    /// kill with an exit, and [`End::Interrupted`] otherwise. A command that
    /// prints more than [`OUTPUT_BUDGET`] is killed the same way and ends
    /// [`End::OutputLimit`]. When the host stops answering, its connection
    /// closes or its start was not confirmed the end is [`End::Interrupted`].
    pub(crate) async fn supervise(
        mut self,
        limit: Duration,
        cancel: &CancellationToken,
        mut on_out: impl FnMut(&[u8]),
        mut on_err: impl FnMut(&[u8]),
    ) -> End {
        let stop = match self.unconfirmed {
            true => Stop::Lost,
            false => match self.poll(limit, cancel, &mut on_out, &mut on_err).await {
                Ok(end) => return end,
                Err(stop) => stop,
            },
        };
        let killed = timeout(HOST_ANSWER, self.host.kill(&self.id)).await;
        self.armed = false;
        let Ok(Ok(chunk)) = killed else {
            return End::Interrupted;
        };
        on_out(chunk.stdout.as_bytes());
        on_err(chunk.stderr.as_bytes());
        match (stop, chunk.exit) {
            // What the command did before is unknown, whatever the kill says.
            (Stop::Lost, _) => End::Interrupted,
            // Without an exit the host did not say the command is dead.
            (_, None) => End::Interrupted,
            // It ended by itself just before the kill.
            (_, Some(exit @ Exit::Code(_))) => End::Exited(exit),
            (Stop::TimedOut, Some(Exit::Signal(_))) => End::TimedOut,
            (Stop::Aborted, Some(Exit::Signal(_))) => End::Aborted,
            (Stop::Flooded, Some(Exit::Signal(_))) => End::OutputLimit,
        }
    }

    /// Read until the command ends (`Ok`) or the loop must stop (`Err`).
    async fn poll(
        &mut self,
        limit: Duration,
        cancel: &CancellationToken,
        on_out: &mut impl FnMut(&[u8]),
        on_err: &mut impl FnMut(&[u8]),
    ) -> Result<End, Stop> {
        let deadline = Instant::now() + limit;
        let mut taken = 0;
        loop {
            let began = Instant::now();
            let read = timeout(READ_WAIT + READ_GRACE, self.host.read(&self.id, READ_WAIT));
            let chunk = tokio::select! {
                biased;
                () = cancel.cancelled() => return Err(Stop::Aborted),
                () = sleep_until(deadline) => return Err(Stop::TimedOut),
                chunk = read => match chunk {
                    Ok(Ok(chunk)) => chunk,
                    _ => return Err(Stop::Lost),
                },
            };
            on_out(chunk.stdout.as_bytes());
            on_err(chunk.stderr.as_bytes());
            if let Some(exit) = chunk.exit {
                self.armed = false;
                return Ok(End::Exited(exit));
            }
            let size = chunk.stdout.len() + chunk.stderr.len();
            taken += size;
            if taken > OUTPUT_BUDGET {
                return Err(Stop::Flooded);
            }
            if size < SMALL_ANSWER {
                // A host that waits as asked is never early; one that does
                // not is paced, and cancel and the deadline still win.
                tokio::select! {
                    biased;
                    () = cancel.cancelled() => return Err(Stop::Aborted),
                    () = sleep_until(deadline) => return Err(Stop::TimedOut),
                    () = sleep_until(began + MIN_POLL) => {}
                }
            }
        }
    }
}

impl Drop for RemoteRun {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        let Ok(runtime) = tokio::runtime::Handle::try_current() else {
            return;
        };
        let (host, id) = (Arc::clone(&self.host), std::mem::take(&mut self.id));
        runtime.spawn(async move {
            let _ = timeout(HOST_ANSWER, host.kill(&id)).await;
        });
    }
}
