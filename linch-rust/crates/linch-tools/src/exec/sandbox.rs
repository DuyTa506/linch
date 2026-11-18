//! The `sandbox` backend: Landlock, a seccomp filter and rlimits around
//! each command, with a private home per session. It needs no root, no
//! user namespace and no daemon: Linux with Landlock ABI 6 (6.12 or
//! later) and util-linux's `prlimit`. This module holds the policy, which
//! every platform reads; the shell is Linux-only.

#[cfg(target_os = "linux")]
mod seccomp;
#[cfg(target_os = "linux")]
mod shell;

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use super::ExecutionError;
#[cfg(target_os = "linux")]
pub(super) use shell::SandboxShell;

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

impl SandboxPolicy {
    /// One sentence for the model on what this policy confines.
    pub(in crate::exec) fn describe(&self) -> String {
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
             sandbox. Each process may use {} of memory, {} s of CPU time and files of \
             {}, and a command at most {} more processes. No host credentials are in \
             the environment.",
            size(self.memory_bytes),
            self.cpu_seconds,
            size(self.file_size_bytes),
            self.max_processes,
        )
    }

    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
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

/// `bytes` in MiB when it is a whole number of them, else in bytes, so the
/// description (and the contract) changes with every value.
fn size(bytes: u64) -> String {
    match bytes % MIB {
        0 => format!("{} MiB", bytes / MIB),
        _ => format!("{bytes} bytes"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The description is how the policy enters the run contract: a change
    /// of any field must change it.
    #[test]
    fn every_field_changes_the_description() {
        let base = SandboxPolicy::default();
        let changed = [
            SandboxPolicy {
                network: true,
                ..base.clone()
            },
            SandboxPolicy {
                memory_bytes: base.memory_bytes + 1,
                ..base.clone()
            },
            SandboxPolicy {
                max_processes: 511,
                ..base.clone()
            },
            SandboxPolicy {
                cpu_seconds: 1799,
                ..base.clone()
            },
            SandboxPolicy {
                file_size_bytes: base.file_size_bytes - 1,
                ..base.clone()
            },
            SandboxPolicy {
                read_paths: vec!["/data".into()],
                ..base.clone()
            },
        ];
        for policy in changed {
            assert_ne!(policy.describe(), base.describe(), "{policy:?}");
        }
    }
}
