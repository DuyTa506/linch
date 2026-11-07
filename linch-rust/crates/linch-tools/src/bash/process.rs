//! One shell command in its own process group, with its output pumped
//! into bounded captures while it runs.

use std::collections::BTreeMap;
use std::io;
use std::path::Path;
use std::pin::pin;
use std::process::{ExitStatus, Stdio};
use std::time::Duration;

use rustix::process::{Pid, Signal, kill_process_group};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use super::capture::Capture;

/// How long to keep reading after the group is killed. Only a process that
/// left the group (`setsid`) can hold a pipe open past this.
const DRAIN: Duration = Duration::from_secs(1);

/// How long to wait for the killed shell to be reaped.
const REAP: Duration = Duration::from_secs(2);

/// Why the command stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum End {
    /// The shell exited on its own.
    Exited(ExitStatus),
    /// The timeout fired first.
    TimedOut,
    /// The call was cancelled first.
    Aborted,
}

/// A finished command.
#[derive(Debug)]
pub(super) struct Finished {
    pub end: End,
    pub stdout: Capture,
    pub stderr: Capture,
}

/// Run `command` with `/bin/sh -c` in `cwd` with exactly `env`.
///
/// The shell leads a new process group. When the shell exits, times out or
/// is cancelled, the whole group gets `SIGKILL`, so nothing it started
/// outlives the call. Dropping the returned future kills the group too.
pub(super) async fn run(
    command: &str,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    timeout: Duration,
    cancel: &CancellationToken,
) -> io::Result<Finished> {
    let mut child = Command::new("/bin/sh")
        .arg("-c")
        .arg(command)
        .current_dir(cwd)
        .env_clear()
        .envs(env)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true)
        .spawn()?;
    // Declared after `child`, so on drop it kills the group before the
    // child handle goes.
    let mut group = Group::of(child.id());
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        unreachable!("both streams are piped");
    };
    let mut out = Capture::default();
    let mut err = Capture::default();
    let deadline = Instant::now() + timeout;
    let end = {
        let mut pumps = pin!(async {
            tokio::join!(pump(stdout, &mut out), pump(stderr, &mut err));
        });
        let mut pumped = false;
        let end = loop {
            tokio::select! {
                biased;
                status = child.wait() => break End::Exited(status?),
                () = cancel.cancelled() => break End::Aborted,
                () = tokio::time::sleep_until(deadline) => break End::TimedOut,
                () = &mut pumps, if !pumped => pumped = true,
            }
        };
        // On timeout or cancel this stops the command; after an exit it
        // stops whatever the command left running in the background.
        group.kill();
        if !pumped {
            let _ = tokio::time::timeout(DRAIN, &mut pumps).await;
        }
        end
    };
    if !matches!(end, End::Exited(_)) {
        let _ = tokio::time::timeout(REAP, child.wait()).await;
    }
    // The shell is reaped (or left to tokio's reaper), so its id may be
    // reused: never signal the group again.
    group.disarm();
    Ok(Finished {
        end,
        stdout: out,
        stderr: err,
    })
}

/// Copy a pipe into `capture` until end of file or a read error.
async fn pump(mut pipe: impl AsyncRead + Unpin, capture: &mut Capture) {
    let mut buffer = vec![0; 64 * 1024];
    while let Ok(read @ 1..) = pipe.read(&mut buffer).await {
        capture.push(&buffer[..read]);
    }
}

/// The command's process group, killed when this guard drops while armed.
struct Group(Option<Pid>);

impl Group {
    /// The group led by process `id`. Ids 0 and 1 never name a group here;
    /// `kill(-1)` would signal every process.
    fn of(id: Option<u32>) -> Self {
        let pid = id
            .and_then(|id| i32::try_from(id).ok())
            .filter(|&id| id > 1)
            .and_then(Pid::from_raw);
        Self(pid)
    }

    /// Send `SIGKILL` to every process in the group. An empty group is fine.
    fn kill(&self) {
        if let Some(pid) = self.0 {
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }

    fn disarm(&mut self) {
        self.0 = None;
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.kill();
    }
}
