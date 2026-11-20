//! The sandbox shell (Linux).
//!
//! Landlock and seccomp restrict the calling *thread*, and a process forked
//! from it inherits both. So each command is forked from a short-lived
//! thread that first restricts itself; the server's other threads stay
//! unrestricted, and no code runs between fork and exec. Rlimits are
//! per process, so `prlimit` sets them and then execs the shell, keeping
//! its pid, its process group and the helper's stdin. The helper that
//! ends what a command started (see `crate::reaper`) is a separate program
//! run inside the same restrictions.

use std::collections::{BTreeMap, HashMap};
use std::fs::DirBuilder;
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use async_trait::async_trait;
use landlock::{
    ABI, Access, AccessFs, AccessNet, CompatLevel, Compatible, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, Scope, path_beneath_rules,
};
use linch_core::SessionId;
use tempfile::TempDir;
use tokio::process::Command;
use tokio::runtime::Handle;
use tokio::sync::oneshot;

use super::{ETC_READ_PATHS, SYSTEM_READ_PATHS, SandboxPolicy, seccomp};
use crate::bash::process::{self, Spawned, Started};
use crate::bash::supervisor;
use crate::exec::{ExecutionError, Launch, Shell};

/// Devices a command may read and write.
const DEVICES: [&str; 5] = [
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
];

/// Where `prlimit` may be.
const PRLIMIT: [&str; 2] = ["/usr/bin/prlimit", "/bin/prlimit"];

/// The Landlock ABI the sandbox requires in full: file access, TCP, and
/// the scoping of signals and abstract Unix sockets.
const LANDLOCK: ABI = ABI::V6;

/// What a command is confined to, shared by every spawn of one backend.
#[derive(Debug)]
struct Rules {
    network: bool,
    root: PathBuf,
    read: Vec<PathBuf>,
    /// The `linch-reaper` helper every command runs under, if found: it
    /// must be executable inside the sandbox.
    reaper: Option<PathBuf>,
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
                &self.reaper,
                AccessFs::Execute | AccessFs::ReadFile,
            ))
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
pub(in crate::exec) struct SandboxShell {
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
    pub(in crate::exec) fn open(
        root: &Path,
        policy: SandboxPolicy,
    ) -> Result<Self, ExecutionError> {
        Self::open_in(root, policy, Path::new("/etc"))
    }

    /// [`Self::open`] with the system configuration at `etc`, so a test
    /// can stand in for `/etc`.
    fn open_in(root: &Path, policy: SandboxPolicy, etc: &Path) -> Result<Self, ExecutionError> {
        if !seccomp::SUPPORTED {
            return Err(ExecutionError::Unsupported("sandbox"));
        }
        policy.validate()?;
        let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
        unprivileged(&status).map_err(ExecutionError::Privileged)?;
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
        read.extend(etc_read_paths(etc));
        read.extend(policy.read_paths.iter().cloned());
        let rules = Arc::new(Rules {
            network: policy.network,
            root: root.to_path_buf(),
            read,
            reaper: supervisor::program().map(|helper| helper.program.clone()),
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

/// [`ETC_READ_PATHS`] under `etc`, each `*` entry expanded to the entries
/// of its directory that start with the rest of its name.
fn etc_read_paths(etc: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    for entry in ETC_READ_PATHS {
        let Some(prefix) = entry.strip_suffix('*') else {
            paths.push(etc.join(entry));
            continue;
        };
        let path = etc.join(prefix);
        let (Some(dir), Some(start)) = (path.parent(), path.file_name()) else {
            continue;
        };
        let start = start.as_encoded_bytes();
        let Ok(entries) = std::fs::read_dir(dir) else {
            continue;
        };
        for entry in entries.flatten() {
            if entry.file_name().as_encoded_bytes().starts_with(start) {
                paths.push(entry.path());
            }
        }
    }
    paths
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
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Started> {
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
                if let Err(Ok(spawned)) = sent.send(job.run(&handle)) {
                    // The call went away while the command started.
                    process::abandon(spawned);
                }
            })?;
        let spawned = received
            .await
            .map_err(|_| io::Error::other("the sandbox spawner thread ended early"))??;
        Ok(Started::Process(spawned))
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
    fn run(self, handle: &Handle) -> io::Result<Spawned> {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.home)?;
        // The helper the command runs under is a process too, but not the
        // command's.
        let helper = u64::from(supervisor::program().is_some());
        let limits = limits(&self.policy, user_tasks().saturating_add(helper));
        self.rules.confine(&self.home).map_err(io::Error::other)?;
        let _runtime = handle.enter();
        let mut command = Command::new(&self.prlimit);
        command.args(&limits).args(&self.shell);
        process::start_shell(command, &self.cwd, &self.env, self.lifeline)
    }
}

/// Tasks (threads) the server's user runs now, which `RLIMIT_NPROC` counts:
/// the `Threads` of every process whose real uid is the user's. Read from
/// `status`, not the `/proc` entry's owner, which is root for a process
/// that is not dumpable (as `linch-server` makes itself).
fn user_tasks() -> u64 {
    let uid = rustix::process::getuid().as_raw().to_string();
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return 0;
    };
    let mut tasks = 0u64;
    for entry in entries.flatten() {
        let numeric = (entry.file_name().to_str())
            .is_some_and(|name| name.bytes().all(|b| b.is_ascii_digit()));
        if !numeric {
            continue;
        }
        let Ok(status) = std::fs::read_to_string(entry.path().join("status")) else {
            continue;
        };
        let field = |name: &str| {
            let line = status.lines().find(|line| line.starts_with(name))?;
            line[name.len()..].split_whitespace().next()
        };
        if field("Uid:") == Some(uid.as_str()) {
            let threads = field("Threads:").and_then(|n| n.parse().ok()).unwrap_or(1);
            tasks = tasks.saturating_add(threads);
        }
    }
    tasks
}

/// Refuse a server whose commands could hold privileges, from its
/// `/proc/self/status`: any uid 0 (a real or saved root uid can be taken
/// back), or a capability in the effective or ambient set (ambient ones
/// pass to every command). Landlock and seccomp bound an unprivileged
/// process; root keeps `RLIMIT_NPROC` off and many ways out. What cannot
/// be read is refused too.
fn unprivileged(status: &str) -> Result<(), String> {
    let field = |name: &str| {
        let line = status.lines().find(|line| line.starts_with(name))?;
        Some(line[name.len()..].split_whitespace().collect::<Vec<_>>())
    };
    let uids: Option<Vec<u32>> = field("Uid:")
        .filter(|uids| uids.len() == 4)
        .and_then(|uids| uids.iter().map(|uid| uid.parse().ok()).collect());
    let Some(uids) = uids else {
        return Err("cannot read the server's user ids".into());
    };
    if uids.contains(&0) {
        return Err("the server runs as root; run it as an unprivileged user".into());
    }
    for set in ["CapEff:", "CapAmb:"] {
        let caps = field(set)
            .and_then(|value| value.first().copied())
            .and_then(|hex| u64::from_str_radix(hex, 16).ok());
        match caps {
            Some(0) => {}
            Some(_) => {
                return Err(format!(
                    "the server holds capabilities ({set} not empty); \
                     run it without any"
                ));
            }
            None => return Err(format!("cannot read the server's {set}")),
        }
    }
    Ok(())
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
            principal: None,
            idempotency_key: None,
            lifeline: true,
        };
        let Spawned {
            mut child, helper, ..
        } = shell.spawn(launch).await.unwrap().into_process();
        let held = (child.stdin.take(), helper);
        let alive = tokio::time::timeout(Duration::from_millis(300), child.wait()).await;
        assert!(
            alive.is_err(),
            "the command runs while the lifeline is held"
        );
        drop(held);
        let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
            .await
            .expect("the watchdog killed the group")
            .unwrap();
        assert_eq!(status.signal(), Some(9));
    }

    /// Run `command` in `shell` and return what it printed.
    async fn output(shell: &SandboxShell, root: &Path, command: &str) -> String {
        let env = BTreeMap::from([("PATH".to_owned(), "/usr/bin:/bin".to_owned())]);
        let session = SessionId::from("s1");
        let launch = Launch {
            command,
            cwd: root,
            env: &env,
            session: &session,
            principal: None,
            idempotency_key: None,
            lifeline: false,
        };
        let Spawned {
            mut child, helper, ..
        } = shell.spawn(launch).await.unwrap().into_process();
        // Closing the helper's socket (or `wait_with_output` closing stdin)
        // stops a supervised command.
        let _held = (child.stdin.take(), helper);
        let out = child.wait_with_output().await.unwrap();
        let mut text = String::from_utf8_lossy(&out.stdout).into_owned();
        text.push_str(&String::from_utf8_lossy(&out.stderr));
        text
    }

    /// Only the listed system configuration is readable: a file of the
    /// configuration directory that is not listed (where host secrets
    /// live) is closed, though its permissions would let the user read it.
    #[tokio::test]
    async fn only_listed_system_configuration_is_readable() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("ws");
        let etc = dir.path().join("etc");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(etc.join("ssl/certs")).unwrap();
        std::fs::create_dir_all(etc.join("python3.99")).unwrap();
        std::fs::write(etc.join("passwd"), "user\n").unwrap();
        std::fs::write(etc.join("ssl/certs/ca.pem"), "cert\n").unwrap();
        std::fs::write(etc.join("python3.99/site.py"), "site\n").unwrap();
        std::fs::write(etc.join("linch-test-secret"), "hidden\n").unwrap();
        std::fs::create_dir_all(etc.join("ssl/private")).unwrap();
        std::fs::write(etc.join("ssl/private/key.pem"), "key\n").unwrap();
        let root = root.canonicalize().unwrap();
        let etc = etc.canonicalize().unwrap();
        let shell = match SandboxShell::open_in(&root, SandboxPolicy::default(), &etc) {
            Ok(shell) => shell,
            Err(error) => return eprintln!("skipped: {error}"),
        };
        let e = etc.display();
        let listed = format!("cat {e}/passwd {e}/ssl/certs/ca.pem {e}/python3.99/site.py");
        assert_eq!(output(&shell, &root, &listed).await, "user\ncert\nsite\n");
        for secret in ["linch-test-secret", "ssl/private/key.pem"] {
            let read = output(&shell, &root, &format!("cat {e}/{secret}")).await;
            assert!(read.contains("Permission denied"), "{secret}: {read}");
        }
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

    fn status(uid: &str, eff: &str, amb: &str) -> String {
        format!(
            "Name:\tlinch-server\nUid:\t{uid}\nGid:\t1000\t1000\t1000\t1000\n\
             CapInh:\t0000000000000000\nCapPrm:\t{eff}\nCapEff:\t{eff}\n\
             CapBnd:\t000001ffffffffff\nCapAmb:\t{amb}\n"
        )
    }

    const NONE: &str = "0000000000000000";

    #[test]
    fn an_unprivileged_server_may_open_a_sandbox() {
        let plain = status("1000\t1000\t1000\t1000", NONE, NONE);
        assert_eq!(unprivileged(&plain), Ok(()));
        // This test process is not root (the sandbox tests need that).
        if rustix::process::geteuid().is_root() {
            return;
        }
        let own = std::fs::read_to_string("/proc/self/status").unwrap();
        assert_eq!(unprivileged(&own), Ok(()));
    }

    /// Root, a root uid it could return to, or any capability would pass
    /// to the command (capabilities through the ambient set): refused.
    #[test]
    fn a_privileged_server_is_refused() {
        let refused = [
            status("0\t0\t0\t0", NONE, NONE),
            status("1000\t0\t1000\t1000", NONE, NONE),
            status("0\t1000\t1000\t1000", NONE, NONE),
            status("1000\t1000\t0\t1000", NONE, NONE),
            status("1000\t1000\t1000\t1000", "0000000000200000", NONE),
            status("1000\t1000\t1000\t1000", NONE, "0000000000002000"),
        ];
        for text in refused {
            assert!(unprivileged(&text).is_err(), "{text}");
        }
    }

    /// What cannot be read is not assumed safe.
    #[test]
    fn an_unreadable_status_is_refused() {
        let missing_caps = "Uid:\t1000\t1000\t1000\t1000\n";
        let missing_uid = format!("CapEff:\t{NONE}\nCapAmb:\t{NONE}\n");
        let garbled = status("1000\t1000\t1000\t1000", "zz", NONE);
        let short_uid = status("1000", NONE, NONE);
        for text in ["", missing_caps, &missing_uid, &garbled, &short_uid] {
            assert!(unprivileged(text).is_err(), "{text:?}");
        }
    }

    /// The count covers at least this process's own threads.
    #[test]
    fn the_user_task_count_includes_this_process() {
        let own = std::fs::read_dir("/proc/self/task").unwrap().count() as u64;
        assert!(user_tasks() >= own);
    }
}
