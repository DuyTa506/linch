//! The `docker` backend: each command in a fresh `docker run --rm`
//! container with Python Linch's limits (2 GiB memory, 2 CPUs, 512 pids,
//! every capability dropped, no new privileges), the workspace mounted at
//! its own path and the network off unless the policy names one.
//!
//! The container is the daemon's, not a child of this process, so killing
//! the `docker` client's process group does not stop it: every end other
//! than the command exiting runs `docker rm -f` on its name, and every
//! command (foreground too) runs under a lifeline whose watchdog kills the
//! client and then removes the container when the server dies. Each
//! container carries a `linch.boot` label naming the server process that
//! started it (host boot id, pid namespace, uid, pid and start time);
//! opening a backend removes labelled containers whose server is provably
//! gone.

use std::collections::{BTreeMap, hash_map::RandomState};
use std::hash::{BuildHasher, Hasher};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::process::Command;

use super::{CLEANUP, Cleanup, ExecutionError, Launch, Shell, wait_at_most};
use crate::bash::process::{self, Spawned, Started};

const MIB: u64 = 1024 * 1024;

/// How each command's container is run. Every field has a default, so
/// `{"backend": "docker"}` is a complete config.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DockerPolicy {
    /// The image; `python:3.12-slim` when absent, as in Python Linch. Name
    /// an immutable digest for a reproducible world.
    #[serde(default = "default_image")]
    pub image: String,
    /// The `docker` client; found on the server's `PATH` when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub docker_path: Option<PathBuf>,
    /// `--network`; `none` when absent (Python Linch leaves Docker's
    /// default bridge on). `none`, `bridge` or a network's name; `host`
    /// and `container:<name>` are refused.
    #[serde(default = "default_network")]
    pub network: String,
    /// `--memory`, in bytes; 2 GiB when absent.
    #[serde(default = "default_memory")]
    pub memory_bytes: u64,
    /// `--cpus`, in thousandths of a CPU; 2000 (2 CPUs) when absent.
    #[serde(default = "default_cpu_millis")]
    pub cpu_millis: u64,
    /// `--pids-limit`; 512 when absent.
    #[serde(default = "default_pids")]
    pub pids_limit: u64,
    /// Whether the container's root file system is read-only (`--read-only`).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub read_only_root: bool,
    /// `--tmpfs` entries, such as `/tmp:size=64m`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tmpfs: Vec<String>,
    /// `--user`; the server's own uid and gid when absent, so files a
    /// command writes in the workspace belong to the user the file tools
    /// run as.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user: Option<String>,
    /// Whether a command may run as root in the container (`user` `0` or
    /// `root`, or a server running as root). Refused when absent.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub allow_root: bool,
}

fn default_image() -> String {
    "python:3.12-slim".into()
}

fn default_network() -> String {
    "none".into()
}

fn default_memory() -> u64 {
    2048 * MIB
}

fn default_cpu_millis() -> u64 {
    2000
}

fn default_pids() -> u64 {
    512
}

impl Default for DockerPolicy {
    fn default() -> Self {
        Self {
            image: default_image(),
            docker_path: None,
            network: default_network(),
            memory_bytes: default_memory(),
            cpu_millis: default_cpu_millis(),
            pids_limit: default_pids(),
            read_only_root: false,
            tmpfs: Vec::new(),
            user: None,
            allow_root: false,
        }
    }
}

/// Variables of `Bash`'s environment that describe the host, not the
/// command: the container does not get them.
const HOST_ONLY: [&str; 5] = ["PATH", "HOME", "USER", "SHELL", "TMPDIR"];

/// Host variables the `docker` client reads to find its daemon.
const CLIENT_ENV: [&str; 7] = [
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "XDG_RUNTIME_DIR",
];

/// Every command's wrapper: the lifeline of [`process::LIFELINE`], whose
/// watchdog first kills the client (`$$`, which `exec` makes the client),
/// so it cannot go on to start the container, then removes the container,
/// then kills the group. `$1` is the client, `$2` the container name, the
/// rest the `docker run`.
const DOCKER_LIFELINE: &str = "exec 3<&0 </dev/null; d=$1 n=$2; shift 2; \
                               (read -r _ <&3; kill -KILL $$; \"$d\" rm -f \"$n\"; kill -KILL 0) \
                               >/dev/null 2>&1 & exec 3<&-; exec \"$@\"";

/// The label naming the server process that started a container.
const OWNER_LABEL: &str = "linch.boot";

impl DockerPolicy {
    /// One sentence for the model on what this policy confines.
    pub(super) fn describe(&self) -> String {
        let user = self.user.as_deref().unwrap_or("the server's user id");
        let network = match self.network.as_str() {
            "none" => "no network".to_owned(),
            other => format!("network {other}"),
        };
        let memory = match self.memory_bytes % MIB {
            0 => format!("{} MiB", self.memory_bytes / MIB),
            _ => format!("{} bytes", self.memory_bytes),
        };
        // Every field shows, so every change of policy changes the contract.
        let mut extra = String::new();
        if self.read_only_root {
            extra.push_str(", read-only root");
        }
        for entry in &self.tmpfs {
            extra.push_str(&format!(", tmpfs {entry}"));
        }
        if let Some(path) = &self.docker_path {
            extra.push_str(&format!(", client {}", path.display()));
        }
        if self.allow_root {
            extra.push_str(", root allowed");
        }
        format!(
            "Commands run in a fresh Docker container each (image {}, {network}, {memory} of \
             memory, {} CPUs, {} processes, no capabilities{extra}) as {user}, with the \
             workspace mounted at its own path; nothing outside the workspace persists \
             between commands, and no host credentials are in the environment.",
            self.image,
            cpus(self.cpu_millis),
            self.pids_limit,
        )
    }

    fn validate(&self) -> Result<(), ExecutionError> {
        let invalid = |message: &str| ExecutionError::Invalid {
            backend: "docker",
            message: message.to_owned(),
        };
        if self.image.trim().is_empty() || self.network.trim().is_empty() {
            return Err(invalid("image and network must not be blank"));
        }
        // `docker run` would read it as an option.
        if self.image.starts_with('-') {
            return Err(invalid("image must not start with '-'"));
        }
        if !network_allowed(&self.network) {
            return Err(invalid(
                "network must be none, bridge or a network's name (not host or container:...)",
            ));
        }
        if self.memory_bytes == 0 || self.cpu_millis == 0 || self.pids_limit == 0 {
            return Err(invalid("limits must be above 0"));
        }
        Ok(())
    }
}

/// `none`, `bridge` or a user-defined network's name: never the host's
/// network stack, nor another container's (`container:<name>`).
fn network_allowed(network: &str) -> bool {
    let mut chars = network.chars();
    let named = chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'));
    named && network != "host"
}

/// Whether `user` (`--user`, `name[:group]`) is root in the container.
fn is_root(user: &str) -> bool {
    let name = user.split(':').next().unwrap_or_default();
    name == "root" || name.parse::<u64>() == Ok(0)
}

/// `--cpus` for `millis` thousandths of a CPU.
fn cpus(millis: u64) -> String {
    format!("{}.{:03}", millis / 1000, millis % 1000)
}

/// Commands run in containers. One per agent.
#[derive(Debug)]
pub(crate) struct DockerShell {
    policy: DockerPolicy,
    docker: PathBuf,
    user: String,
    client_env: BTreeMap<String, String>,
    owner: Option<Owner>,
    started: AtomicU64,
}

impl DockerShell {
    /// A Docker shell for `policy` over the workspace at `root`. Only the
    /// client is looked for; the daemon is reached per command, and a
    /// command fails when it is down.
    pub(super) fn open(policy: DockerPolicy, root: &Path) -> Result<Self, ExecutionError> {
        policy.validate()?;
        let invalid = |message: String| ExecutionError::Invalid {
            backend: "docker",
            message,
        };
        // `--mount` reads its value as comma-separated fields.
        let path = root.display().to_string();
        if path.contains([',', '"', '\n']) {
            return Err(invalid(format!(
                "the workspace path {path} has a comma, quote or newline, which --mount cannot take"
            )));
        }
        let docker = match &policy.docker_path {
            Some(path) => path.clone(),
            None => find("docker").ok_or_else(|| ExecutionError::Missing {
                backend: "docker",
                program: "docker".into(),
            })?,
        };
        let user = policy.user.clone().unwrap_or_else(|| {
            let uid = rustix::process::getuid().as_raw();
            let gid = rustix::process::getgid().as_raw();
            format!("{uid}:{gid}")
        });
        if user.is_empty() || user.starts_with(['-', ':']) {
            return Err(invalid(format!("user {user:?} is not a user")));
        }
        if is_root(&user) && !policy.allow_root {
            return Err(invalid(format!(
                "commands would run as root in the container (user {user}); \
                 set allow_root to allow it"
            )));
        }
        let client_env = (CLIENT_ENV.iter())
            .filter_map(|&key| Some((key.to_owned(), std::env::var(key).ok()?)))
            .collect();
        Ok(Self {
            policy,
            docker,
            user,
            client_env,
            owner: Owner::this_process(),
            started: AtomicU64::new(0),
        })
    }

    /// Remove, on a thread of its own and at most [`CLEANUP`] per `docker`
    /// call, the labelled containers whose server process is gone: one
    /// left behind when even the lifeline's watchdog could not remove it.
    /// Best effort; a daemon that is down or slow changes nothing.
    pub(super) fn reap_stale(&self) {
        let Some(owner) = self.owner.clone() else {
            return;
        };
        let docker = self.docker.clone();
        let env = self.client_env.clone();
        let _ = std::thread::Builder::new()
            .name("linch-docker-reap".into())
            .spawn(move || {
                let format = format!("{{{{.ID}}}} {{{{.Label \"{OWNER_LABEL}\"}}}}");
                let filter = format!("label={OWNER_LABEL}");
                let list = ["ps", "-a", "--filter", &filter, "--format", &format];
                let Some(listing) = run_bounded(&docker, &list, &env) else {
                    return;
                };
                let stale = stale(&listing, &owner, Owner::alive);
                if !stale.is_empty() {
                    let mut remove = vec!["rm", "-f"];
                    remove.extend(stale.iter().map(String::as_str));
                    run_bounded(&docker, &remove, &env);
                }
            });
    }

    /// A container name no other command of this process has had.
    fn fresh_name(&self) -> String {
        let n = self.started.fetch_add(1, Ordering::Relaxed);
        let mut hasher = RandomState::new().build_hasher();
        hasher.write_u64(n);
        format!("linch-{:016x}", hasher.finish())
    }

    /// The `docker run` arguments for `launch` in container `name`.
    fn run_args(&self, name: &str, launch: &Launch<'_>) -> Vec<String> {
        let policy = &self.policy;
        // `--quiet`: an image pull's progress is not the command's output.
        let mut args: Vec<String> = ["run", "--rm", "--quiet", "--name", name]
            .map(String::from)
            .into();
        let mut flag = |flag: &str, value: String| {
            args.push(flag.to_owned());
            args.push(value);
        };
        if let Some(owner) = &self.owner {
            flag("--label", format!("{OWNER_LABEL}={owner}"));
        }
        flag("--memory", policy.memory_bytes.to_string());
        flag("--cpus", cpus(policy.cpu_millis));
        flag("--pids-limit", policy.pids_limit.to_string());
        flag("--cap-drop", "ALL".into());
        flag("--security-opt", "no-new-privileges".into());
        flag("--network", policy.network.clone());
        for entry in &policy.tmpfs {
            flag("--tmpfs", entry.clone());
        }
        flag("--user", self.user.clone());
        // `--env NAME` takes the value from the client's environment, so
        // no value is on a command line.
        for key in launch.env.keys() {
            if !HOST_ONLY.contains(&key.as_str()) {
                flag("--env", key.clone());
            }
        }
        let cwd = launch.cwd.display().to_string();
        flag("-w", cwd.clone());
        flag("--mount", format!("type=bind,src={cwd},dst={cwd}"));
        if policy.read_only_root {
            args.push("--read-only".into());
        }
        args.push(policy.image.clone());
        args.extend(["sh", "-c", launch.command].map(String::from));
        args
    }

    /// The program and arguments the process group starts with: the client
    /// under [`DOCKER_LIFELINE`], for a foreground command too, since a
    /// container outlives a killed server.
    fn argv(&self, name: &str, launch: &Launch<'_>) -> (PathBuf, Vec<String>) {
        let run = self.run_args(name, launch);
        let docker = self.docker.display().to_string();
        let mut args: Vec<String> = vec!["-c".into(), DOCKER_LIFELINE.into(), "linch-bash".into()];
        args.extend([docker.clone(), name.to_owned(), docker]);
        args.extend(run);
        (PathBuf::from("/bin/sh"), args)
    }

    /// The client's environment: `Bash`'s, which `--env` reads, and the
    /// host's `DOCKER_*` variables.
    fn client_env(&self, launch: &Launch<'_>) -> BTreeMap<String, String> {
        let mut env = launch.env.clone();
        env.extend(self.client_env.clone());
        env
    }
}

#[async_trait]
impl Shell for DockerShell {
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Started> {
        let name = self.fresh_name();
        let (program, args) = self.argv(&name, &launch);
        let env = self.client_env(&launch);
        let mut command = Command::new(program);
        command.args(args);
        // Always a lifeline: `supervise` holds its write end for a
        // foreground command, a background one's owner for the rest.
        let child = process::start(command, launch.cwd, &env, true)?;
        let cleanup = Cleanup {
            program: self.docker.clone(),
            args: vec!["rm".into(), "-f".into(), name],
            env,
        };
        Ok(Started::Process(Spawned {
            child,
            cleanup: Some(cleanup),
            helper: None,
        }))
    }

    fn program(&self) -> &str {
        "docker"
    }
}

/// The server process that starts containers, as their `linch.boot` label
/// names it: the host's boot id, the pid namespace (the boot id is shared
/// by every container of a host, pids are not), the user (`hidepid` hides
/// other users' processes), the pid and the pid's start time (clock ticks
/// since boot). Together they never name another process.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Owner {
    boot: String,
    pid_namespace: u64,
    uid: u32,
    pid: u32,
    start: u64,
}

impl Owner {
    /// This process, or `None` when `/proc` cannot say (then nothing is
    /// labelled or reaped).
    fn this_process() -> Option<Self> {
        use std::os::unix::fs::MetadataExt;
        let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?;
        let pid = std::process::id();
        Some(Self {
            boot: boot.trim().to_owned(),
            pid_namespace: std::fs::metadata("/proc/self/ns/pid").ok()?.ino(),
            uid: rustix::process::getuid().as_raw(),
            pid,
            start: start_time(pid)?,
        })
    }

    fn parse(label: &str) -> Option<Self> {
        let mut parts = label.split(':');
        let owner = Self {
            boot: parts.next()?.to_owned(),
            pid_namespace: parts.next()?.parse().ok()?,
            uid: parts.next()?.parse().ok()?,
            pid: parts.next()?.parse().ok()?,
            start: parts.next()?.parse().ok()?,
        };
        parts.next().is_none().then_some(owner)
    }

    /// Whether this process can tell if `other` runs: same host boot, pid
    /// namespace and user.
    fn sees(&self, other: &Self) -> bool {
        self.boot == other.boot
            && self.pid_namespace == other.pid_namespace
            && self.uid == other.uid
    }

    /// Whether the process `owner` names still runs on this host.
    fn alive(owner: &Self) -> bool {
        start_time(owner.pid) == Some(owner.start)
    }
}

impl std::fmt::Display for Owner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}:{}:{}:{}:{}",
            self.boot, self.pid_namespace, self.uid, self.pid, self.start
        )
    }
}

/// When process `pid` started, in clock ticks since boot (field 22 of its
/// `stat`, counted after the parenthesised name, which may hold spaces).
fn start_time(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (_, rest) = stat.rsplit_once(')')?;
    rest.split_whitespace().nth(19)?.parse().ok()
}

/// The containers of `listing` (`ID LABEL` lines) to remove: those whose
/// server `me` can see (see [`Owner::sees`]) is not `me` and not `alive`.
/// A container of another host, another container or another user, of an
/// unreadable label, or of a live server is kept.
fn stale(listing: &str, me: &Owner, alive: impl Fn(&Owner) -> bool) -> Vec<String> {
    let mut stale = Vec::new();
    for line in listing.lines() {
        let Some((id, label)) = line.trim().split_once(' ') else {
            continue;
        };
        let Some(owner) = Owner::parse(label.trim()) else {
            continue;
        };
        if me.sees(&owner) && owner != *me && !alive(&owner) {
            stale.push(id.to_owned());
        }
    }
    stale
}

/// Run `docker` with `args` and the client environment for at most
/// [`CLEANUP`]; its stdout when it exits 0.
fn run_bounded(docker: &Path, args: &[&str], env: &BTreeMap<String, String>) -> Option<String> {
    let out = tempfile::tempfile().ok()?;
    let mut child = std::process::Command::new(docker)
        .args(args)
        .env_clear()
        .envs(env)
        .stdin(std::process::Stdio::null())
        .stdout(out.try_clone().ok()?)
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let status = wait_at_most(&mut child, CLEANUP)?;
    if !status.success() {
        return None;
    }
    let mut out = out;
    out.seek(SeekFrom::Start(0)).ok()?;
    let mut text = String::new();
    out.read_to_string(&mut text).ok()?;
    Some(text)
}

/// `program` on the server's `PATH`.
fn find(program: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(program))
        .find(|candidate| candidate.is_file())
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::time::Duration;

    use linch_core::SessionId;

    use super::*;

    fn owner() -> Owner {
        Owner::parse("b00t:4026531836:1000:42:7").unwrap()
    }

    fn open(policy: DockerPolicy) -> Result<DockerShell, ExecutionError> {
        DockerShell::open(
            DockerPolicy {
                docker_path: Some("/usr/bin/docker".into()),
                ..policy
            },
            Path::new("/work/space"),
        )
    }

    fn shell(policy: DockerPolicy) -> DockerShell {
        let mut shell = open(DockerPolicy {
            user: Some("1000:1000".into()),
            ..policy
        })
        .unwrap();
        shell.owner = Some(owner());
        shell
    }

    fn launch<'a>(env: &'a BTreeMap<String, String>, lifeline: bool) -> Launch<'a> {
        static SESSION: std::sync::LazyLock<SessionId> =
            std::sync::LazyLock::new(|| SessionId::from("s1"));
        Launch {
            command: "echo hi",
            cwd: Path::new("/work/space"),
            env,
            session: &SESSION,
            principal: None,
            idempotency_key: None,
            lifeline,
        }
    }

    /// Python Linch's limits, the network off, the workspace bind-mounted
    /// at its own path, the server named on a label, no pull progress, and
    /// only the command's own variables passed by name; under a lifeline
    /// that removes the container, for a foreground command too.
    #[test]
    fn a_foreground_command_runs_with_the_policy() {
        let env = BTreeMap::from([
            ("PATH".to_owned(), "/usr/bin".to_owned()),
            ("HOME".to_owned(), "/home/me".to_owned()),
            ("LANG".to_owned(), "C.UTF-8".to_owned()),
        ]);
        let (program, args) = shell(DockerPolicy::default()).argv("linch-x", &launch(&env, false));
        assert_eq!(program, Path::new("/bin/sh"));
        assert_eq!(args[..3], ["-c", DOCKER_LIFELINE, "linch-bash"]);
        assert_eq!(
            args[3..6],
            ["/usr/bin/docker", "linch-x", "/usr/bin/docker"]
        );
        let expected = "run --rm --quiet --name linch-x --label linch.boot=b00t:4026531836:1000:42:7 \
                        --memory 2147483648 --cpus 2.000 \
                        --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges \
                        --network none --user 1000:1000 --env LANG -w /work/space \
                        --mount type=bind,src=/work/space,dst=/work/space \
                        python:3.12-slim sh -c";
        assert_eq!(args[6..args.len() - 1].join(" "), expected);
        assert_eq!(args.last().unwrap(), "echo hi");
    }

    #[test]
    fn a_policy_cannot_inject_options_or_take_the_host() {
        let refused = [
            DockerPolicy {
                image: "--privileged".into(),
                ..DockerPolicy::default()
            },
            DockerPolicy {
                network: "host".into(),
                ..DockerPolicy::default()
            },
            DockerPolicy {
                network: "container:db".into(),
                ..DockerPolicy::default()
            },
            DockerPolicy {
                network: "-x".into(),
                ..DockerPolicy::default()
            },
        ];
        for policy in refused {
            let opened = open(DockerPolicy {
                user: Some("1000:1000".into()),
                ..policy.clone()
            });
            assert!(
                matches!(opened, Err(ExecutionError::Invalid { .. })),
                "{policy:?}"
            );
        }
        for network in ["none", "bridge", "my-net_1.x"] {
            let policy = DockerPolicy {
                network: network.into(),
                user: Some("1000:1000".into()),
                ..DockerPolicy::default()
            };
            assert!(open(policy).is_ok(), "{network}");
        }
        let comma = DockerShell::open(
            DockerPolicy {
                docker_path: Some("/usr/bin/docker".into()),
                user: Some("1000:1000".into()),
                ..DockerPolicy::default()
            },
            Path::new("/work/a,readonly=false"),
        );
        assert!(matches!(comma, Err(ExecutionError::Invalid { .. })));
    }

    /// Root in the container needs `allow_root`, whether the policy names
    /// it or the server runs as root (its uid is the default).
    #[test]
    fn root_in_the_container_needs_allow_root() {
        for user in ["0", "0:0", "00", "0:1000", "root", "root:root"] {
            let policy = DockerPolicy {
                user: Some(user.into()),
                ..DockerPolicy::default()
            };
            let refused = open(policy.clone());
            assert!(
                matches!(refused, Err(ExecutionError::Invalid { .. })),
                "{user}"
            );
            let allowed = DockerPolicy {
                allow_root: true,
                ..policy
            };
            assert!(open(allowed).is_ok(), "{user}");
        }
        for user in ["1000", "1000:0", "nobody", "-1"] {
            let policy = DockerPolicy {
                user: Some(user.into()),
                ..DockerPolicy::default()
            };
            let opened = open(policy);
            assert_eq!(opened.is_ok(), user != "-1", "{user}");
        }
        if rustix::process::getuid().is_root() {
            assert!(open(DockerPolicy::default()).is_err());
        }
    }

    /// Only a container of this host boot whose server process is gone is
    /// stale: never one of a live server, of this one, or of another host.
    #[test]
    fn only_containers_of_a_dead_server_are_reaped() {
        let me = owner();
        // The boot id is the host's, so servers in other containers (other
        // pid namespaces) or of other users (hidden by `hidepid`) share it
        // but cannot be seen: never judged dead.
        let listing = "c1 b00t:4026531836:1000:42:7\n\
                       c2 b00t:4026531836:1000:43:9\n\
                       c3 b00t:4026531836:1000:44:9\n\
                       c4 other:4026531836:1000:43:9\n\
                       c5 garbage\n\
                       c6 b00t:4026531836:1000:43:9:extra\n\
                       c7 b00t:4026532999:1000:43:9\n\
                       c8 b00t:4026531836:1001:43:9\n";
        let alive = |owner: &Owner| owner.pid == 44;
        assert_eq!(stale(listing, &me, alive), ["c2"]);
        let own = Owner::this_process().unwrap();
        assert!(Owner::alive(&own));
        assert_eq!(Owner::parse(&own.to_string()), Some(own.clone()));
        let restarted = Owner {
            start: own.start + 1,
            ..own
        };
        assert!(!Owner::alive(&restarted));
    }

    #[test]
    fn a_background_command_runs_under_a_lifeline_that_removes_its_container() {
        let env = BTreeMap::new();
        let policy = DockerPolicy {
            read_only_root: true,
            tmpfs: vec!["/tmp".into()],
            network: "bridge".into(),
            cpu_millis: 1500,
            ..DockerPolicy::default()
        };
        let (program, args) = shell(policy).argv("linch-y", &launch(&env, true));
        assert_eq!(program, Path::new("/bin/sh"));
        assert_eq!(args[..3], ["-c", DOCKER_LIFELINE, "linch-bash"]);
        assert_eq!(
            args[3..6],
            ["/usr/bin/docker", "linch-y", "/usr/bin/docker"]
        );
        let run = args[6..].join(" ");
        assert!(run.contains("--cpus 1.500"), "{run}");
        assert!(run.contains("--network bridge --tmpfs /tmp"), "{run}");
        assert!(run.contains("--read-only python:3.12-slim"), "{run}");
    }

    /// A stand-in `docker` that records each `rm` and whether the `run`
    /// client was still alive then (a zombie counts as gone).
    const FAKE_DOCKER: &str = "#!/bin/sh\n\
        case \"$1\" in\n\
        run) echo $$ > \"$LOG.pid\"; exec sleep 30 ;;\n\
        rm) pid=$(cat \"$LOG.pid\"); state=$(cut -d' ' -f3 /proc/$pid/stat 2>/dev/null); \
            case \"$state\" in ''|Z) echo \"gone $*\" >> \"$LOG\" ;; *) echo \"alive $*\" >> \"$LOG\" ;; esac ;;\n\
        esac\n";

    /// The server dying fires the lifeline of a foreground command too,
    /// and the watchdog kills the client before it removes the container,
    /// so a client still creating it cannot start it after the removal.
    #[tokio::test]
    async fn the_lifeline_kills_the_client_then_removes_the_container() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let docker = dir.path().join("docker");
        std::fs::write(&docker, FAKE_DOCKER).unwrap();
        std::fs::set_permissions(&docker, std::fs::Permissions::from_mode(0o755)).unwrap();
        let log = dir.path().join("log");
        let policy = DockerPolicy {
            docker_path: Some(docker),
            user: Some("1000:1000".into()),
            ..DockerPolicy::default()
        };
        let shell = DockerShell::open(policy, dir.path()).unwrap();
        let env = BTreeMap::from([("LOG".to_owned(), log.display().to_string())]);
        let mut launch = launch(&env, false);
        launch.cwd = dir.path();
        let Spawned { mut child, .. } = shell.spawn(launch).await.unwrap().into_process();
        for _ in 0..250 {
            if log.with_extension("pid").exists() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        // The server dies: its end of the lifeline closes.
        drop(child.stdin.take());
        let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
        for _ in 0..250 {
            if std::fs::read_to_string(&log).is_ok_and(|text| text.contains("rm")) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let removed = std::fs::read_to_string(&log).unwrap_or_default();
        assert!(removed.starts_with("gone rm -f linch-"), "{removed:?}");
    }

    #[test]
    fn the_description_names_the_limits() {
        let text = DockerPolicy::default().describe();
        assert!(
            text.contains("image python:3.12-slim, no network, 2048 MiB"),
            "{text}"
        );
        assert!(text.contains("2.000 CPUs, 512 processes"), "{text}");
    }

    /// The description is how the policy enters the run contract: a change
    /// of any field must change it.
    #[test]
    fn every_field_changes_the_description() {
        let base = DockerPolicy::default();
        let changed = [
            DockerPolicy {
                image: "alpine:3.20".into(),
                ..base.clone()
            },
            DockerPolicy {
                docker_path: Some("/opt/docker".into()),
                ..base.clone()
            },
            DockerPolicy {
                network: "bridge".into(),
                ..base.clone()
            },
            DockerPolicy {
                memory_bytes: base.memory_bytes + 1,
                ..base.clone()
            },
            DockerPolicy {
                cpu_millis: 1999,
                ..base.clone()
            },
            DockerPolicy {
                pids_limit: 511,
                ..base.clone()
            },
            DockerPolicy {
                read_only_root: true,
                ..base.clone()
            },
            DockerPolicy {
                tmpfs: vec!["/tmp".into()],
                ..base.clone()
            },
            DockerPolicy {
                user: Some("0:0".into()),
                ..base.clone()
            },
            DockerPolicy {
                allow_root: true,
                ..base.clone()
            },
        ];
        for policy in changed {
            assert_ne!(policy.describe(), base.describe(), "{policy:?}");
        }
    }
}
