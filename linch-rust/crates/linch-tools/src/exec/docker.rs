//! The `docker` backend: each command in a fresh `docker run --rm`
//! container with Python Linch's limits (2 GiB memory, 2 CPUs, 512 pids,
//! every capability dropped, no new privileges), the workspace mounted at
//! its own path and the network off unless the policy names one.
//!
//! The container is the daemon's, not a child of this process, so killing
//! the `docker` client's process group does not stop it: every end other
//! than the command exiting runs `docker rm -f` on its name, and a
//! background command's lifeline watchdog does too when the server dies.

use std::collections::{BTreeMap, hash_map::RandomState};
use std::hash::{BuildHasher, Hasher};
use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::process::Command;

use super::{Cleanup, ExecutionError, Launch, Shell};
use crate::bash::process::{self, Spawned};

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
    /// default bridge on).
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

/// A background command's wrapper: the lifeline of
/// [`process::LIFELINE`], whose watchdog also removes the container. `$1`
/// is the client, `$2` the container name, the rest the `docker run`.
const DOCKER_LIFELINE: &str = "exec 3<&0 </dev/null; d=$1 n=$2; shift 2; \
                               (read -r _ <&3; \"$d\" rm -f \"$n\"; kill -KILL 0) \
                               >/dev/null 2>&1 & exec 3<&-; exec \"$@\"";

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
        if self.memory_bytes == 0 || self.cpu_millis == 0 || self.pids_limit == 0 {
            return Err(invalid("limits must be above 0"));
        }
        Ok(())
    }
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
    started: AtomicU64,
}

impl DockerShell {
    /// A Docker shell for `policy`. Only the client is looked for; the
    /// daemon is reached per command, and a command fails when it is down.
    pub(super) fn open(policy: DockerPolicy) -> Result<Self, ExecutionError> {
        policy.validate()?;
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
        let client_env = (CLIENT_ENV.iter())
            .filter_map(|&key| Some((key.to_owned(), std::env::var(key).ok()?)))
            .collect();
        Ok(Self {
            policy,
            docker,
            user,
            client_env,
            started: AtomicU64::new(0),
        })
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
        // `--quiet`: an image pull's progress would land in the command's
        // stderr.
        let mut args: Vec<String> = ["run", "--rm", "--quiet", "--name", name]
            .map(String::from)
            .into();
        let mut flag = |flag: &str, value: String| {
            args.push(flag.to_owned());
            args.push(value);
        };
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
        flag("-v", format!("{cwd}:{cwd}"));
        if policy.read_only_root {
            args.push("--read-only".into());
        }
        args.push(policy.image.clone());
        args.extend(["sh", "-c", launch.command].map(String::from));
        args
    }

    /// The program and arguments the process group starts with.
    fn argv(&self, name: &str, launch: &Launch<'_>) -> (PathBuf, Vec<String>) {
        let run = self.run_args(name, launch);
        if !launch.lifeline {
            return (self.docker.clone(), run);
        }
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
    async fn spawn(&self, launch: Launch<'_>) -> io::Result<Spawned> {
        let name = self.fresh_name();
        let (program, args) = self.argv(&name, &launch);
        let env = self.client_env(&launch);
        let mut command = Command::new(program);
        command.args(args);
        let child = process::start(command, launch.cwd, &env, launch.lifeline)?;
        let cleanup = Cleanup {
            program: self.docker.clone(),
            args: vec!["rm".into(), "-f".into(), name],
            env,
        };
        Ok(Spawned {
            child,
            cleanup: Some(cleanup),
        })
    }

    fn program(&self) -> &str {
        "docker"
    }
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

    use linch_core::SessionId;

    use super::*;

    fn shell(policy: DockerPolicy) -> DockerShell {
        DockerShell::open(DockerPolicy {
            docker_path: Some("/usr/bin/docker".into()),
            user: Some("1000:1000".into()),
            ..policy
        })
        .unwrap()
    }

    fn launch<'a>(env: &'a BTreeMap<String, String>, lifeline: bool) -> Launch<'a> {
        static SESSION: std::sync::LazyLock<SessionId> =
            std::sync::LazyLock::new(|| SessionId::from("s1"));
        Launch {
            command: "echo hi",
            cwd: Path::new("/work/space"),
            env,
            session: &SESSION,
            lifeline,
        }
    }

    /// Python Linch's limits, the network off, the workspace at its own
    /// path, and only the command's own variables passed by name.
    #[test]
    fn a_foreground_command_runs_with_the_policy() {
        let env = BTreeMap::from([
            ("PATH".to_owned(), "/usr/bin".to_owned()),
            ("HOME".to_owned(), "/home/me".to_owned()),
            ("LANG".to_owned(), "C.UTF-8".to_owned()),
        ]);
        let (program, args) = shell(DockerPolicy::default()).argv("linch-x", &launch(&env, false));
        assert_eq!(program, Path::new("/usr/bin/docker"));
        let expected = "run --rm --quiet --name linch-x --memory 2147483648 --cpus 2.000 \
                        --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges \
                        --network none --user 1000:1000 --env LANG -w /work/space \
                        -v /work/space:/work/space python:3.12-slim sh -c";
        assert_eq!(args[..args.len() - 1].join(" "), expected);
        assert_eq!(args.last().unwrap(), "echo hi");
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
        ];
        for policy in changed {
            assert_ne!(policy.describe(), base.describe(), "{policy:?}");
        }
    }
}
