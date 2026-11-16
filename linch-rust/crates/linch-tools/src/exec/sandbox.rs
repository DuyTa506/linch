//! The `sandbox` backend: Landlock, a seccomp filter and rlimits around
//! each command, with a private home per session. It needs no root, no
//! user namespace and no daemon: Linux with Landlock ABI 6 (6.12 or
//! later) and util-linux's `prlimit`.
//!
//! Landlock and seccomp restrict the calling *thread*, and a process forked
//! from it inherits both. So each command is forked from a short-lived
//! thread that first restricts itself; the server's other threads stay
//! unrestricted, and no code runs between fork and exec. Rlimits are
//! per process, so `prlimit` sets them and then execs the shell, keeping
//! its pid, its process group and the lifeline's stdin.

use std::collections::{BTreeMap, HashMap};
use std::fs::DirBuilder;
use std::io;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use async_trait::async_trait;
use landlock::{
    ABI, Access, AccessFs, AccessNet, CompatLevel, Compatible, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, Scope, path_beneath_rules,
};
use linch_core::SessionId;
use rustix::process::{Pid, Signal, kill_process_group};
use serde::{Deserialize, Serialize};
use tempfile::TempDir;
use tokio::process::{Child, Command};
use tokio::runtime::Handle;
use tokio::sync::oneshot;

use super::{ExecutionError, Launch, Shell, seccomp};
use crate::bash::process::{self, Spawned};

const MIB: u64 = 1024 * 1024;

/// What a sandboxed command may do. Every field has a default, so
/// `{"backend": "sandbox"}` is a complete config.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxPolicy {
    /// Whether commands may open network sockets. Off when absent: every
    /// `socket` call fails, and Landlock refuses TCP bind and connect.
    #[serde(default)]
    pub network: bool,
    /// Address space of each process (`RLIMIT_AS`), in bytes; 2 GiB when
    /// absent, Python's Docker default.
    #[serde(default = "default_memory")]
    pub memory_bytes: u64,
    /// How many processes and threads a command may add to those the
    /// server's user already runs (`RLIMIT_NPROC` is per user, so the
    /// limit is set to the user's count at the start plus this); 512 when
    /// absent.
    #[serde(default = "default_processes")]
    pub max_processes: u64,
    /// CPU time of each process (`RLIMIT_CPU`), in seconds; 1800 when
    /// absent, Bash's longest timeout.
    #[serde(default = "default_cpu")]
    pub cpu_seconds: u64,
    /// Largest file a process may write (`RLIMIT_FSIZE`), in bytes; 1 GiB
    /// when absent.
    #[serde(default = "default_file_size")]
    pub file_size_bytes: u64,
    /// Absolute directories or files a command may also read and execute,
    /// beyond the system directories, the workspace and its home.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub read_paths: Vec<PathBuf>,
}

fn default_memory() -> u64 {
    2048 * MIB
}

fn default_processes() -> u64 {
    512
}

fn default_cpu() -> u64 {
    1800
}

fn default_file_size() -> u64 {
    1024 * MIB
}

impl Default for SandboxPolicy {
    fn default() -> Self {
        Self {
            network: false,
            memory_bytes: default_memory(),
            max_processes: default_processes(),
            cpu_seconds: default_cpu(),
            file_size_bytes: default_file_size(),
            read_paths: Vec::new(),
        }
    }
}

/// Directories a command may read and execute: the system's programs,
/// libraries and configuration, and `/proc` and `/sys` (Landlock's ptrace
/// rule keeps another process's `environ`, `mem` and `fd` closed).
pub const SYSTEM_READ_PATHS: [&str; 11] = [
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt", "/proc", "/sys",
];

/// Devices a command may read and write.
const DEVICES: [&str; 6] = [
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
];

/// Where `prlimit` may be.
const PRLIMIT: [&str; 2] = ["/usr/bin/prlimit", "/bin/prlimit"];

/// The Landlock ABI the sandbox requires in full: file access, TCP, and
/// the scoping of signals and abstract Unix sockets.
const LANDLOCK: ABI = ABI::V6;

impl SandboxPolicy {
    /// One sentence for the model on what this policy confines.
    pub(super) fn describe(&self) -> String {
        let network = match self.network {
            true => "may use the network (but not Unix sockets)",
            false => "have no network",
        };
        let mut read = SYSTEM_READ_PATHS.join(", ");
        for path in &self.read_paths {
            read.push_str(", ");
            read.push_str(&path.display().to_string());
        }
        format!(
            "Commands run sandboxed: they can read only the workspace, their private $HOME \
             (also $TMPDIR, kept until the session closes) and {read}; write only the \
             workspace and that home; {network}; and cannot signal processes outside the \
             sandbox. Each process may use {} MiB of memory, {} s of CPU time and files of \
             {} MiB, and a command at most {} more processes. No host credentials are in \
             the environment.",
            self.memory_bytes / MIB,
            self.cpu_seconds,
            self.file_size_bytes / MIB,
            self.max_processes,
        )
    }

    fn validate(&self) -> Result<(), ExecutionError> {
        let invalid = |message: &str| ExecutionError::Invalid {
            backend: "sandbox",
            message: message.to_owned(),
        };
        if self.memory_bytes == 0
            || self.max_processes == 0
            || self.cpu_seconds == 0
            || self.file_size_bytes == 0
        {
            return Err(invalid("limits must be above 0"));
        }
        if let Some(path) = self.read_paths.iter().find(|path| !path.is_absolute()) {
            return Err(invalid(&format!(
                "read path {} is not absolute",
                path.display()
            )));
        }
        Ok(())
    }
}

/// What a command is confined to, shared by every spawn of one backend.
#[derive(Debug)]
struct Rules {
    network: bool,
    root: PathBuf,
    read: Vec<PathBuf>,
}

impl Rules {
    /// Restrict the calling thread to these rules plus `home`, and apply
    /// the seccomp filter. Fails unless Landlock enforces all of it.
    fn confine(&self, home: &Path) -> Result<(), String> {
        let error = |error: landlock::RulesetError| error.to_string();
        let mut ruleset = Ruleset::default()
            .set_compatibility(CompatLevel::HardRequirement)
            .handle_access(AccessFs::from_all(LANDLOCK))
            .map_err(error)?;
        if !self.network {
            ruleset = ruleset
                .handle_access(AccessNet::from_all(LANDLOCK))
                .map_err(error)?;
        }
        let status = ruleset
            .scope(Scope::from_all(LANDLOCK))
            .map_err(error)?
            .create()
            .map_err(error)?
            .add_rules(path_beneath_rules(
                &self.read,
                AccessFs::from_read(LANDLOCK),
            ))
            .map_err(error)?
            .add_rules(path_beneath_rules(DEVICES, AccessFs::from_file(LANDLOCK)))
            .map_err(error)?
            .add_rules(path_beneath_rules(
                [self.root.as_path(), home],
                AccessFs::from_all(LANDLOCK),
            ))
            .map_err(error)?
            .restrict_self()
            .map_err(error)?;
        if status.ruleset != RulesetStatus::FullyEnforced || !status.no_new_privs {
            return Err(format!("Landlock status {status:?}"));
        }
        seccomp::apply(self.network)
    }
}

/// Commands run under Landlock, seccomp and `prlimit`. One per agent: it
/// owns the private homes of its sessions under a scratch directory that
/// goes with it.
#[derive(Debug)]
pub(crate) struct SandboxShell {
    policy: SandboxPolicy,
    prlimit: PathBuf,
    rules: Arc<Rules>,
    scratch: TempDir,
    homes: Mutex<HashMap<SessionId, PathBuf>>,
    next: AtomicU64,
}

impl SandboxShell {
    /// A sandbox for the workspace at `root`, after checking on a throwaway
    /// thread that the kernel enforces the whole policy.
    pub(super) fn open(root: &Path, policy: SandboxPolicy) -> Result<Self, ExecutionError> {
        if !cfg!(target_os = "linux") || !seccomp::SUPPORTED {
            return Err(ExecutionError::Unsupported("sandbox"));
        }
        policy.validate()?;
        let prlimit = (PRLIMIT.iter().map(PathBuf::from))
            .find(|path| path.is_file())
            .ok_or_else(|| ExecutionError::Missing {
                backend: "sandbox",
                program: "prlimit (util-linux)".into(),
            })?;
        let io = |source| ExecutionError::Io {
            backend: "sandbox",
            source,
        };
        let mut read: Vec<PathBuf> = SYSTEM_READ_PATHS.iter().map(PathBuf::from).collect();
        read.extend(policy.read_paths.iter().cloned());
        if policy.network {
            // `/etc/resolv.conf` is often a link into `/run`.
            read.extend(std::fs::canonicalize("/etc/resolv.conf").ok());
        }
        let rules = Arc::new(Rules {
            network: policy.network,
            root: root.to_path_buf(),
            read,
        });
        let scratch = tempfile::Builder::new()
            .prefix("linch-sandbox-")
            .tempdir()
            .map_err(io)?;
        let probe = Arc::clone(&rules);
        let home = scratch.path().to_path_buf();
        let checked = std::thread::Builder::new()
            .name("linch-sandbox-probe".into())
            .spawn(move || probe.confine(&home))
            .map_err(io)?
            .join()
            .unwrap_or_else(|_| Err("the probe thread panicked".into()));
        checked.map_err(ExecutionError::Landlock)?;
        Ok(Self {
            policy,
            prlimit,
            rules,
            scratch,
            homes: Mutex::default(),
            next: AtomicU64::new(0),
        })
    }

    /// The private home of `session`, named now and created by the spawn.
    fn home(&self, session: &SessionId) -> PathBuf {
        let mut homes = self.homes.lock().unwrap_or_else(PoisonError::into_inner);
        let home = homes.entry(session.clone()).or_insert_with(|| {
            let n = self.next.fetch_add(1, Ordering::Relaxed);
            self.scratch.path().join(format!("s{n}"))
        });
        home.clone()
    }
}

/// `prlimit`'s arguments for `policy`, `nproc` counted from the `tasks`
/// the server's user runs now, then the shell.
fn limits(policy: &SandboxPolicy, tasks: u64) -> Vec<String> {
    vec![
        format!("--as={}", policy.memory_bytes),
        // `SIGXCPU` at the soft limit; `SIGKILL` a second later for a
        // process that ignores it.
        format!(
            "--cpu={}:{}",
            policy.cpu_seconds,
            policy.cpu_seconds.saturating_add(1)
        ),
        format!("--fsize={}", policy.file_size_bytes),
        format!("--nproc={}", tasks.saturating_add(policy.max_processes)),
        "--".into(),
        "/bin/sh".into(),
    ]
}

#[async_trait]
impl Shell for SandboxShell {
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Spawned> {
        let home = self.home(launch.session);
        let mut env = launch.env.clone();
        for key in ["HOME", "TMPDIR"] {
            env.insert(key.into(), home.display().to_string());
        }
        let shell: Vec<String> = (process::shell_args(launch.command, launch.lifeline).iter())
            .map(|arg| (*arg).to_owned())
            .collect();
        let job = Job {
            rules: Arc::clone(&self.rules),
            prlimit: self.prlimit.clone(),
            policy: self.policy.clone(),
            shell,
            cwd: launch.cwd.to_path_buf(),
            env,
            home,
            lifeline: launch.lifeline,
        };
        let handle = Handle::current();
        let (sent, received) = oneshot::channel();
        std::thread::Builder::new()
            .name("linch-sandbox".into())
            .spawn(move || {
                if let Err(Ok(child)) = sent.send(job.run(&handle)) {
                    // The call went away while the command started.
                    kill(&child);
                }
            })?;
        let child = received
            .await
            .map_err(|_| io::Error::other("the sandbox spawner thread ended early"))??;
        Ok(Spawned {
            child,
            cleanup: None,
        })
    }

    fn close_session(&self, session: &SessionId) {
        let home = self
            .homes
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(session);
        let Some(home) = home else {
            return;
        };
        let remove = move || {
            let _ = std::fs::remove_dir_all(home);
        };
        match Handle::try_current() {
            Ok(handle) => drop(handle.spawn_blocking(remove)),
            Err(_) => remove(),
        }
    }
}

/// One command to start on a spawner thread.
struct Job {
    rules: Arc<Rules>,
    prlimit: PathBuf,
    policy: SandboxPolicy,
    shell: Vec<String>,
    cwd: PathBuf,
    env: BTreeMap<String, String>,
    home: PathBuf,
    lifeline: bool,
}

impl Job {
    /// On a fresh thread: make the home, count the user's tasks, restrict
    /// this thread, and start `prlimit` (which execs the shell).
    fn run(self, handle: &Handle) -> io::Result<Child> {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.home)?;
        let limits = limits(&self.policy, user_tasks());
        self.rules.confine(&self.home).map_err(io::Error::other)?;
        let _runtime = handle.enter();
        let mut command = Command::new(&self.prlimit);
        command.args(&limits).args(&self.shell);
        process::start(command, &self.cwd, &self.env, self.lifeline)
    }
}

/// Tasks (threads) the server's user runs now, which `RLIMIT_NPROC` counts:
/// `/proc` entries owned by the user, one per thread.
fn user_tasks() -> u64 {
    let uid = rustix::process::getuid().as_raw();
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return 0;
    };
    let mut tasks = 0u64;
    for entry in entries.flatten() {
        let numeric = (entry.file_name().to_str())
            .is_some_and(|name| name.bytes().all(|b| b.is_ascii_digit()));
        if !numeric || !entry.metadata().is_ok_and(|meta| meta.uid() == uid) {
            continue;
        }
        let threads = std::fs::read_dir(entry.path().join("task")).map_or(1, |dir| dir.count());
        tasks = tasks.saturating_add(threads as u64);
    }
    tasks
}

/// Kill the group a started command leads.
fn kill(child: &Child) {
    let pid = (child.id())
        .and_then(|id| i32::try_from(id).ok())
        .filter(|&id| id > 1)
        .and_then(Pid::from_raw);
    if let Some(pid) = pid {
        let _ = kill_process_group(pid, Signal::KILL);
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::process::ExitStatusExt;
    use std::time::Duration;

    use super::*;

    /// The lifeline works inside the sandbox: the server dying (its end
    /// of the pipe closing) kills the sandboxed command's whole group.
    #[tokio::test]
    async fn closing_the_lifeline_kills_a_sandboxed_group() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        let shell = match SandboxShell::open(&root, SandboxPolicy::default()) {
            Ok(shell) => shell,
            Err(error) => return eprintln!("skipped: {error}"),
        };
        let env = BTreeMap::from([("PATH".to_owned(), "/usr/bin:/bin".to_owned())]);
        let session = SessionId::from("s1");
        let launch = Launch {
            command: "sleep 30",
            cwd: &root,
            env: &env,
            session: &session,
            lifeline: true,
        };
        let Spawned { mut child, .. } = shell.spawn(launch).await.unwrap();
        let mut held = child.stdin.take();
        let alive = tokio::time::timeout(Duration::from_millis(300), child.wait()).await;
        assert!(
            alive.is_err(),
            "the command runs while the lifeline is held"
        );
        held.take();
        let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
            .await
            .expect("the watchdog killed the group")
            .unwrap();
        assert_eq!(status.signal(), Some(9));
    }

    #[test]
    fn prlimit_gets_every_limit_then_the_shell() {
        let policy = SandboxPolicy::default();
        assert_eq!(
            limits(&policy, 100),
            [
                "--as=2147483648",
                "--cpu=1800:1801",
                "--fsize=1073741824",
                "--nproc=612",
                "--",
                "/bin/sh"
            ]
        );
    }
}
