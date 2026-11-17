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
use tokio::process::{Child, Command};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use super::capture::Capture;
use crate::exec::Cleanup;

/// How long to keep reading after the group is killed. Only a process that
/// left the group (`setsid`) can hold a pipe open past this.
const DRAIN: Duration = Duration::from_secs(1);

/// How long to wait for the killed shell to be reaped.
const REAP: Duration = Duration::from_secs(2);

/// The shell a background command starts under. Its stdin is a pipe whose
/// write end only this process holds: the shell moves it to fd 3 for a
/// watchdog in the command's group and gives the command `/dev/null`. When
/// this process dies, even by `SIGKILL`, the pipe reaches end of file and
/// the watchdog kills the whole group, so a background command never
/// outlives the server. `exec` keeps the shell's pid, the group leader's.
/// The watchdog uses only builtins (nothing ever writes the pipe, so
/// `read` returns at end of file), so it works whatever the `PATH`.
pub(crate) const LIFELINE: &str = "exec 3<&0 </dev/null; (read -r _ <&3; kill -KILL 0) >/dev/null 2>&1 & \
                        exec 3<&-; exec /bin/sh -c \"$1\"";

/// Why the command stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum End {
    /// The shell exited on its own.
    Exited(ExitStatus),
    /// The timeout fired first.
    TimedOut,
    /// The call was cancelled (or the process killed) first.
    Aborted,
}

/// A finished command.
#[derive(Debug)]
pub(super) struct Finished {
    pub end: End,
    pub stdout: Capture,
    pub stderr: Capture,
}

/// Supervise a foreground command an execution backend started, keeping
/// the captured output.
///
/// The command leads a new process group. When the shell exits, times out or
/// is cancelled, the whole group gets `SIGKILL`, so nothing it started
/// outlives the call. Dropping the returned future kills the group too.
pub(super) async fn run(
    spawned: Spawned,
    timeout: Duration,
    cancel: &CancellationToken,
) -> io::Result<Finished> {
    let mut out = Capture::default();
    let mut err = Capture::default();
    let end = supervise(
        spawned,
        timeout,
        cancel,
        |chunk| out.push(chunk),
        |chunk| err.push(chunk),
    )
    .await?;
    Ok(Finished {
        end,
        stdout: out,
        stderr: err,
    })
}

/// A command the execution backend started, with what to run once its
/// group is killed before it exited (a container to remove).
#[derive(Debug)]
pub(crate) struct Spawned {
    pub child: Child,
    pub cleanup: Option<Cleanup>,
}

/// The arguments of `/bin/sh` for `command`: `-c` and the command, or with
/// `lifeline` the [`LIFELINE`] wrapper that runs it.
pub(crate) fn shell_args(command: &str, lifeline: bool) -> Vec<&str> {
    match lifeline {
        true => vec!["-c", LIFELINE, "linch-bash", command],
        false => vec!["-c", command],
    }
}

/// Start `command` under `/bin/sh` in `cwd` with exactly `env`, as the
/// leader of a new process group, stdout and stderr piped. With `lifeline`
/// the command runs under [`LIFELINE`], so it dies with this process;
/// otherwise its stdin is closed.
pub(crate) fn spawn(
    command: &str,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Child> {
    let mut shell = Command::new("/bin/sh");
    shell.args(shell_args(command, lifeline));
    start(shell, cwd, env, lifeline)
}

/// Start `program` in `cwd` with exactly `env`, as the leader of a new
/// process group, stdout and stderr piped, stdin a pipe with `lifeline`
/// (the caller holds its write end) and closed without.
///
/// Every descriptor above stderr is first marked close-on-exec, so one
/// this process holds without it (inherited from its parent, or opened by
/// a library) does not pass to the command; Landlock checks only opens,
/// so a passed descriptor would reach past a sandbox.
pub(crate) fn start(
    mut program: Command,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Child> {
    let stdin = match lifeline {
        true => Stdio::piped(),
        false => Stdio::null(),
    };
    close_fds::set_fds_cloexec_threadsafe(3, &[]);
    program
        .stdin(stdin)
        .current_dir(cwd)
        .env_clear()
        .envs(env)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true)
        .spawn()
}

/// Pump `child`'s output into `on_out` and `on_err` until it exits, times
/// out or `cancel` fires; then kill its whole group, drain what is left,
/// reap the shell and, unless it exited, run the cleanup. Dropping the
/// future kills the group too and starts the cleanup.
pub(super) async fn supervise(
    spawned: Spawned,
    timeout: Duration,
    cancel: &CancellationToken,
    mut on_out: impl FnMut(&[u8]),
    mut on_err: impl FnMut(&[u8]),
) -> io::Result<End> {
    let Spawned { mut child, cleanup } = spawned;
    // Declared after `child`, so on drop it kills the group before the
    // child handle goes.
    let mut group = Group::of(child.id(), cleanup);
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        unreachable!("both streams are piped");
    };
    // A lifeline's write end, held out of `child`: `Child::wait` closes the
    // stdin it holds, which would fire the watchdog at once.
    let _lifeline = child.stdin.take();
    let deadline = Instant::now() + timeout;
    let end = {
        let mut pumps = pin!(async {
            tokio::join!(pump(stdout, &mut on_out), pump(stderr, &mut on_err));
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
        group.clean().await;
    }
    // The shell is reaped (or left to tokio's reaper), so its id may be
    // reused: never signal the group again.
    group.disarm();
    Ok(end)
}

/// Copy a pipe into `push` until end of file or a read error.
async fn pump(mut pipe: impl AsyncRead + Unpin, push: &mut impl FnMut(&[u8])) {
    let mut buffer = vec![0; 64 * 1024];
    while let Ok(read @ 1..) = pipe.read(&mut buffer).await {
        push(&buffer[..read]);
    }
}

/// The command's process group, killed when this guard drops while armed,
/// with the cleanup to run after a kill.
struct Group(Option<Pid>, Option<Cleanup>);

impl Group {
    /// The group led by process `id`. Ids 0 and 1 never name a group here;
    /// `kill(-1)` would signal every process.
    fn of(id: Option<u32>, cleanup: Option<Cleanup>) -> Self {
        let pid = id
            .and_then(|id| i32::try_from(id).ok())
            .filter(|&id| id > 1)
            .and_then(Pid::from_raw);
        Self(pid, cleanup)
    }

    /// Send `SIGKILL` to every process in the group. An empty group is fine.
    fn kill(&self) {
        if let Some(pid) = self.0 {
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }

    /// Run the cleanup, if any, and wait for it.
    async fn clean(&mut self) {
        if let Some(cleanup) = self.1.take() {
            cleanup.run().await;
        }
    }

    fn disarm(&mut self) {
        self.0 = None;
        self.1 = None;
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.kill();
        if let Some(cleanup) = self.1.take() {
            cleanup.detach();
        }
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::process::ExitStatusExt;

    use super::*;

    /// The server dying closes the lifeline's write end; the watchdog then
    /// kills the whole group, with nobody signalling it. It needs nothing
    /// on the `PATH`.
    #[tokio::test]
    async fn closing_the_lifeline_kills_the_group() {
        let dir = tempfile::tempdir().unwrap();
        let env = BTreeMap::from([("PATH".to_owned(), "/nonexistent".to_owned())]);
        let mut child = spawn("/bin/sleep 30", dir.path(), &env, true).unwrap();
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
}
