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
const LIFELINE: &str = "exec 3<&0 </dev/null; (read -r _ <&3; kill -KILL 0) >/dev/null 2>&1 & \
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

/// Run `command` with `/bin/sh -c` in `cwd` with exactly `env`, stdin
/// closed, keeping the captured output.
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
    let child = spawn(command, cwd, env, false)?;
    let mut out = Capture::default();
    let mut err = Capture::default();
    let end = supervise(
        child,
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

/// Start `command` under `/bin/sh` in `cwd` with exactly `env`, as the
/// leader of a new process group, stdout and stderr piped. With `lifeline`
/// the command runs under [`LIFELINE`], so it dies with this process;
/// otherwise its stdin is closed.
pub(super) fn spawn(
    command: &str,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Child> {
    let mut shell = Command::new("/bin/sh");
    if lifeline {
        shell
            .arg("-c")
            .arg(LIFELINE)
            .arg("linch-bash")
            .arg(command)
            .stdin(Stdio::piped());
    } else {
        shell.arg("-c").arg(command).stdin(Stdio::null());
    }
    shell
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
/// out or `cancel` fires; then kill its whole group, drain what is left
/// and reap the shell. Dropping the future kills the group too.
pub(super) async fn supervise(
    mut child: Child,
    timeout: Duration,
    cancel: &CancellationToken,
    mut on_out: impl FnMut(&[u8]),
    mut on_err: impl FnMut(&[u8]),
) -> io::Result<End> {
    // Declared after `child`, so on drop it kills the group before the
    // child handle goes.
    let mut group = Group::of(child.id());
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
