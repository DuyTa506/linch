//! Execution backends: where `Bash` runs its commands, next to the file
//! layer the file tools use.
//!
//! An [`ExecutionBackend`] is one execution world: a [`LocalFs`] confined
//! to the workspace root, which `Read`, `Write`, `Edit`, `Grep` and `Glob`
//! go through, and a shell that `Bash` (foreground and background) starts
//! commands with. [`crate::workspace_tools`] builds every tool from one
//! backend, so the choice applies to all of them.
//!
//! | Backend | Commands run |
//! |---|---|
//! | `local` | Directly as the process's user (`LocalShell`) |
//! | `sandbox` | Under Landlock, a seccomp filter and rlimits, with a private home per session ([`SandboxPolicy`]) |
//! | `docker` | In a fresh container per command, the workspace mounted at the same path ([`DockerPolicy`]) |
//! | `remote` | On a host the agent's client supplies ([`RemotePolicy`], [`RemoteExec`]) |
//!
//! The file tools stay in this process for every backend: the workspace is
//! the same directory on the host (bind-mounted at its own path in a
//! container), so a file `Bash` writes is the file `Read` reads. A remote
//! host is somewhere else, so its backend registers no file tools unless
//! its policy says the host shares the workspace.

mod docker;
mod remote;
mod sandbox;

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::SessionId;
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::process::Command;

pub use docker::DockerPolicy;
pub(crate) use remote::RemoteRun;
pub use remote::{
    Exit, READ_WAIT, RemoteChunk, RemoteCommand, RemoteError, RemoteExec, RemotePolicy,
};
pub use sandbox::SandboxPolicy;

use crate::LocalFs;
use crate::bash::DEADLINE_MARGIN;
use crate::bash::process::{self, Spawned, Started};

/// Which execution backend an agent's tools use, as config data.
///
/// `{"backend": "local"}` is the default. The others carry their policy:
/// `{"backend": "sandbox", "network": false, ...}` (see [`SandboxPolicy`])
/// `{"backend": "docker", "image": "...", ...}` (see [`DockerPolicy`]) and
/// `{"backend": "remote", ...}` (see [`RemotePolicy`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "backend", rename_all = "snake_case", deny_unknown_fields)]
pub enum ExecutionConfig {
    /// Commands run directly as the process's user.
    Local {},
    /// Commands run under Landlock, a seccomp filter and rlimits.
    Sandbox(SandboxPolicy),
    /// Commands run in a Docker container.
    Docker(DockerPolicy),
    /// Commands run on a host the agent's client supplies.
    Remote(RemotePolicy),
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self::Local {}
    }
}

impl ExecutionConfig {
    /// Whether this is the default `local` backend.
    pub fn is_local(&self) -> bool {
        matches!(self, Self::Local {})
    }

    /// The backend's name: `local`, `sandbox`, `docker` or `remote`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Local {} => "local",
            Self::Sandbox(_) => "sandbox",
            Self::Docker(_) => "docker",
            Self::Remote(_) => "remote",
        }
    }
}

/// Why an execution backend could not be opened.
#[derive(Debug, Error)]
pub enum ExecutionError {
    /// The kernel cannot enforce the sandbox; nothing would be confined.
    #[error("the sandbox backend needs Landlock ABI 6 (Linux 6.12 or later), enforced: {0}")]
    Landlock(String),
    /// The server is root or holds capabilities, which a sandboxed command
    /// would keep.
    #[error("the sandbox backend refuses a privileged server: {0}")]
    Privileged(String),
    /// A program the backend runs is missing.
    #[error("the {backend} backend needs {program}, which was not found")]
    Missing {
        /// The backend.
        backend: &'static str,
        /// The program.
        program: String,
    },
    /// The `remote` backend was opened with no host to run the commands:
    /// it needs a live connection, which a worker or a schedule has none of.
    #[error(
        "the remote backend needs a connected host to run its commands: open the agent over a \
         client connection that serves exec.start, exec.read and exec.kill; it cannot run in a \
         worker or from a schedule"
    )]
    NoHost,
    /// The backend does not run on this platform.
    #[error("the {0} backend is not supported on this platform")]
    Unsupported(&'static str),
    /// The policy is not valid.
    #[error("invalid {backend} policy: {message}")]
    Invalid {
        /// The backend.
        backend: &'static str,
        /// What is wrong.
        message: String,
    },
    /// Setting the backend up failed.
    #[error("could not set up the {backend} backend: {source}")]
    Io {
        /// The backend.
        backend: &'static str,
        /// The underlying error.
        #[source]
        source: io::Error,
    },
}

/// One execution world: the workspace's file layer and the shell `Bash`
/// starts commands with. Owned by one agent; a sandbox's private homes and
/// a container's names live in it, never in a process global.
#[derive(Clone)]
pub struct ExecutionBackend {
    fs: Arc<LocalFs>,
    shell: Arc<dyn Shell>,
    describe: Option<String>,
    config: ExecutionConfig,
}

impl ExecutionBackend {
    /// The local backend: commands run directly as the process's user, as
    /// `Bash` always did.
    pub fn local(fs: Arc<LocalFs>) -> Self {
        Self {
            fs,
            shell: Arc::new(LocalShell),
            describe: None,
            config: ExecutionConfig::Local {},
        }
    }

    /// The backend `config` names over `fs`, with no host: the `remote`
    /// backend fails (see [`Self::open_with_host`]).
    ///
    /// Blocks on the file system and, for the sandbox, starts a thread that
    /// checks the kernel enforces the whole policy, so call it from a
    /// blocking task.
    ///
    /// Raises:
    ///     ExecutionError: If the backend cannot confine what its policy
    ///         says on this host (no Landlock ABI 6, no `prlimit`, no
    ///         `docker`), its policy is invalid, or it is `remote`.
    pub fn open(fs: Arc<LocalFs>, config: &ExecutionConfig) -> Result<Self, ExecutionError> {
        Self::open_with_host(fs, config, None)
    }

    /// The backend `config` names over `fs`; a `remote` one runs its
    /// commands through `host`.
    ///
    /// Raises:
    ///     ExecutionError: As for [`Self::open`], and [`ExecutionError::NoHost`]
    ///         when `config` is `remote` and `host` is `None`.
    pub fn open_with_host(
        fs: Arc<LocalFs>,
        config: &ExecutionConfig,
        host: Option<Arc<dyn RemoteExec>>,
    ) -> Result<Self, ExecutionError> {
        let (shell, describe): (Arc<dyn Shell>, _) = match config {
            ExecutionConfig::Local {} => return Ok(Self::local(fs)),
            ExecutionConfig::Sandbox(policy) => {
                (sandbox_shell(fs.root(), policy)?, policy.describe())
            }
            ExecutionConfig::Docker(policy) => {
                let shell = docker::DockerShell::open(policy.clone(), fs.root())?;
                shell.reap_stale();
                (Arc::new(shell), policy.describe())
            }
            ExecutionConfig::Remote(policy) => {
                let host = host.ok_or(ExecutionError::NoHost)?;
                let shell = remote::RemoteShell::new(host, policy.clone());
                (Arc::new(shell), policy.describe())
            }
        };
        Ok(Self {
            fs,
            shell,
            describe: Some(describe),
            config: config.clone(),
        })
    }

    /// The workspace's file layer.
    pub fn fs(&self) -> &Arc<LocalFs> {
        &self.fs
    }

    /// The canonical workspace root, where commands start.
    pub fn root(&self) -> &Path {
        self.fs.root()
    }

    /// The config this backend was opened with.
    pub fn config(&self) -> &ExecutionConfig {
        &self.config
    }

    /// Whether commands run confined: `false` for the local backend.
    pub fn sandboxed(&self) -> bool {
        !self.config.is_local()
    }

    /// Whether the file tools may act in this backend's world: `false`
    /// for a `remote` backend whose host does not share the workspace,
    /// since the file tools would read and write the server's directory
    /// while commands run somewhere else.
    pub fn has_file_tools(&self) -> bool {
        match &self.config {
            ExecutionConfig::Remote(policy) => policy.shared_workspace,
            _ => true,
        }
    }

    /// What the backend confines, in one sentence for the model; `None` for
    /// the local backend. `Bash` ends its description with it.
    pub fn describe(&self) -> Option<&str> {
        self.describe.as_deref()
    }

    /// Drop what the backend keeps for `session` (a sandbox's private
    /// home). Sync, so a session-close callback can call it.
    pub fn close_session(&self, session: &SessionId) {
        self.shell.close_session(session);
    }

    pub(crate) fn shell(&self) -> Arc<dyn Shell> {
        Arc::clone(&self.shell)
    }
}

#[cfg(target_os = "linux")]
fn sandbox_shell(root: &Path, policy: &SandboxPolicy) -> Result<Arc<dyn Shell>, ExecutionError> {
    Ok(Arc::new(sandbox::SandboxShell::open(root, policy.clone())?))
}

/// Landlock and seccomp are Linux's.
#[cfg(not(target_os = "linux"))]
fn sandbox_shell(_root: &Path, _policy: &SandboxPolicy) -> Result<Arc<dyn Shell>, ExecutionError> {
    Err(ExecutionError::Unsupported("sandbox"))
}

impl fmt::Debug for ExecutionBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ExecutionBackend")
            .field("root", &self.root())
            .field("config", &self.config)
            .finish()
    }
}

/// One command to start.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Launch<'a> {
    /// The shell command.
    pub command: &'a str,
    /// Where it starts: the workspace root.
    pub cwd: &'a Path,
    /// The environment `Bash` built: its allowlisted host variables and
    /// what the host added.
    pub env: &'a BTreeMap<String, String>,
    /// The session the call belongs to (the sandbox keys its homes on it).
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub session: &'a SessionId,
    /// Whether the command runs under the lifeline wrapper (background).
    pub lifeline: bool,
}

/// The shell half of an execution backend: how a command is started. A
/// local command must lead a new process group with stdout and stderr
/// piped, and with `lifeline` read a pipe on stdin whose end of file kills
/// the group ([`process::LIFELINE`]); `supervise` does the rest the same
/// way for every backend. A remote command is run by its host, and
/// supervised through the host's answers.
#[async_trait]
pub(crate) trait Shell: Send + Sync + fmt::Debug {
    /// Start `launch`.
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Started>;

    /// What a spawn error names as the program that did not start.
    fn program(&self) -> &str {
        "/bin/sh"
    }

    /// How long before a call's deadline (`left` away) a command of this
    /// shell must end, so its kill and the result fit before the scheduler
    /// drops the call.
    fn deadline_margin(&self, _left: Duration) -> Duration {
        DEADLINE_MARGIN
    }

    /// Drop what the shell keeps for `session`.
    fn close_session(&self, _session: &SessionId) {}
}

/// Commands run directly as the process's user: `/bin/sh -c` in the root,
/// exactly as `Bash` did before execution backends.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct LocalShell;

#[async_trait]
impl Shell for LocalShell {
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Started> {
        let child = process::spawn(launch.command, launch.cwd, launch.env, launch.lifeline)?;
        Ok(Started::Process(Spawned {
            child,
            cleanup: None,
        }))
    }
}

/// How long a cleanup command may take.
const CLEANUP: Duration = Duration::from_secs(10);

/// A command to run after a command's group was killed before it exited,
/// for what the group does not hold (a container the daemon runs).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Cleanup {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
}

impl Cleanup {
    fn command(&self) -> std::process::Command {
        let mut command = std::process::Command::new(&self.program);
        command
            .args(&self.args)
            .env_clear()
            .envs(&self.env)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        command
    }

    /// Run it and wait, at most [`CLEANUP`].
    pub(crate) async fn run(&self) {
        let mut command = Command::from(self.command());
        if let Ok(mut child) = command.kill_on_drop(true).spawn() {
            let _ = tokio::time::timeout(CLEANUP, child.wait()).await;
        }
    }

    /// Start it without waiting (from a `Drop`): on the Tokio runtime when
    /// there is one, else on a thread of its own, so a guard dropped
    /// outside a runtime still cleans up.
    pub(crate) fn detach(&self) {
        let Ok(handle) = tokio::runtime::Handle::try_current() else {
            let mut command = self.command();
            let _ = std::thread::Builder::new()
                .name("linch-cleanup".into())
                .spawn(move || {
                    if let Ok(mut child) = command.spawn() {
                        let _ = wait_at_most(&mut child, CLEANUP);
                    }
                });
            return;
        };
        let mut command = Command::from(self.command());
        handle.spawn(async move {
            if let Ok(mut child) = command.spawn() {
                let _ = tokio::time::timeout(CLEANUP, child.wait()).await;
            }
        });
    }
}

/// Wait for `child` from a plain thread for at most `limit`, then kill it;
/// either way it is reaped. Its exit status when it exited in time.
fn wait_at_most(
    child: &mut std::process::Child,
    limit: Duration,
) -> Option<std::process::ExitStatus> {
    let deadline = std::time::Instant::now() + limit;
    while std::time::Instant::now() < deadline {
        match child.try_wait() {
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Ok(status) => return status,
            Err(_) => return None,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A guard dropped outside a Tokio runtime (a plain thread, a runtime
    /// shutting down) still removes what its command left.
    #[test]
    fn a_cleanup_detached_without_a_runtime_still_runs() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("cleaned");
        let cleanup = Cleanup {
            program: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                "touch \"$0\"".into(),
                marker.display().to_string(),
            ],
            env: BTreeMap::new(),
        };
        cleanup.detach();
        for _ in 0..250 {
            if marker.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(marker.exists(), "the cleanup did not run");
    }
}
