//! One shell command in its own process group, with its output pumped
//! into bounded captures while it runs.
//!
//! On Linux a command also runs under a helper (found by
//! [`super::supervisor`], see [`crate::reaper`]), which holds everything the
//! command starts, including what leaves its process group, and ends it with
//! the command. The helper tells the server the shell's pid, so the server
//! signals the shell and its group itself, whether or not the helper still
//! runs. Without a helper, and elsewhere, the process group is all that holds
//! a command.

use std::collections::BTreeMap;
use std::io::{self, ErrorKind, Read};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::pin::pin;
use std::process::Stdio;
use std::sync::Once;
use std::time::Duration;

use rustix::process::{Pid, Signal, kill_process_group};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::{Child, Command};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use super::capture::Capture;
use super::supervisor;
use crate::exec::{Cleanup, Exit, RemoteRun};
use crate::reaper::ReaperCommand;

/// How long to keep reading after the command is stopped. Without the
/// helper, only a process that left the group (`setsid`) can hold a pipe
/// open past this.
const DRAIN: Duration = Duration::from_secs(1);

/// How long to wait for the stopped command (its helper, or its killed
/// shell) to be reaped.
const REAP: Duration = Duration::from_secs(2);

/// How long to wait for a helper to say which shell it started.
const HANDSHAKE: Duration = Duration::from_secs(5);

/// The shell a background command starts under. Its stdin is a pipe (or,
/// under the helper, a socket) whose other end only this process holds: the
/// shell moves it to fd 3 for a watchdog in the command's group and gives
/// the command `/dev/null`. When this process dies, even by `SIGKILL`, the
/// pipe reaches end of file and the watchdog kills the whole group, so a
/// background command never outlives the server. `exec` keeps the shell's
/// pid, the group leader's. The watchdog uses only builtins (nothing ever
/// writes the pipe, so `read` returns at end of file), so it works whatever
/// the `PATH`.
pub(crate) const LIFELINE: &str = "exec 3<&0 </dev/null; (read -r _ <&3; kill -KILL 0) >/dev/null 2>&1 & \
                        exec 3<&-; exec /bin/sh -c \"$1\"";

/// Why the command stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum End {
    /// The shell exited on its own.
    Exited(Exit),
    /// The timeout fired first.
    TimedOut,
    /// The call was cancelled (or the process killed) first.
    Aborted,
    /// A remote command printed more than the server takes, and the host
    /// confirmed the kill.
    OutputLimit,
    /// A remote host stopped answering, or did not confirm a kill, so how
    /// the command ended is not known.
    Interrupted,
}

impl From<std::process::ExitStatus> for Exit {
    fn from(status: std::process::ExitStatus) -> Self {
        match status.code() {
            Some(code) => Self::Code(code),
            None => Self::Signal(status.signal().unwrap_or(0)),
        }
    }
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
/// The shell leads a new process group. When it exits, times out or is
/// cancelled, its group is killed and, under a helper, everything else it
/// started too, so nothing it started outlives the call. Dropping the
/// returned future stops the command too.
pub(super) async fn run(
    spawned: Started,
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

/// A command the execution backend started on this machine, with what to
/// run once its group is killed before it exited (a container to remove).
#[derive(Debug)]
pub(crate) struct Spawned {
    /// The shell, or the helper that runs it.
    pub child: Child,
    pub cleanup: Option<Cleanup>,
    /// Set when `child` is a helper.
    pub helper: Option<Helper>,
}

/// What the server holds of a helper that runs a command.
#[derive(Debug)]
pub(crate) struct Helper {
    /// The server's end of the helper's stdin, a socket: closing it stops
    /// the command and everything under it.
    pub link: UnixStream,
    /// The shell's pid, which leads the command's process group; `None`
    /// when it had already ended by the time it was checked.
    pub shell: Option<u32>,
}

/// A command an execution backend started: a child of this process, or
/// one a host runs elsewhere.
// One value per command, moved once into `supervise`: boxing buys nothing.
#[allow(clippy::large_enum_variant)]
#[derive(Debug)]
pub(crate) enum Started {
    /// A child of this process, supervised through its process group.
    Process(Spawned),
    /// A command on a remote host, supervised through its connection.
    Remote(RemoteRun),
}

impl From<Spawned> for Started {
    fn from(spawned: Spawned) -> Self {
        Self::Process(spawned)
    }
}

impl Started {
    /// The command's process id on this machine (the shell's, not the
    /// helper's); `None` for a remote one.
    pub(super) fn pid(&self) -> Option<u32> {
        match self {
            Self::Process(spawned) => (spawned.helper.as_ref())
                .and_then(|helper| helper.shell)
                .or_else(|| spawned.child.id()),
            Self::Remote(_) => None,
        }
    }

    /// The local command, for a test of a local shell.
    #[cfg(test)]
    pub(crate) fn into_process(self) -> Spawned {
        match self {
            Self::Process(spawned) => spawned,
            Self::Remote(_) => panic!("a local command"),
        }
    }
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
/// leader of a new process group, stdout and stderr piped. See
/// [`start_shell`] for `lifeline`.
///
/// Blocks until a helper has started the shell, so call it from a blocking
/// task.
pub(crate) fn spawn(
    command: &str,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Spawned> {
    let mut shell = Command::new("/bin/sh");
    shell.args(shell_args(command, lifeline));
    start_shell(supervisor::program(), shell, cwd, env, lifeline)
}

/// Whether [`spawn`] blocks: a helper has to say which shell it started.
pub(crate) fn blocks() -> bool {
    supervisor::program().is_some()
}

/// Start the shell `program` runs in `cwd` with exactly `env`, as the
/// leader of a new process group, stdout and stderr piped.
///
/// Where there is a `helper`, the child is the helper, which runs `program`
/// and tells this process its pid. Its stdin is a socket this process holds
/// the other end of: closing it (the caller stopping the command, or dying)
/// ends the command and everything it started. With `lifeline` the
/// shell gets the socket too (see [`LIFELINE`]), so it dies with this
/// process even if its helper was killed. A helper that cannot start, or
/// does not run the command, is passed over: the command then runs as it
/// does without one.
///
/// Without a helper, `lifeline` gives the shell a pipe on stdin whose write
/// end this process holds; otherwise its stdin is closed.
///
/// Blocks until a helper has started the shell, so call it from a blocking
/// task.
pub(crate) fn start_shell(
    helper: Option<&ReaperCommand>,
    program: Command,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Spawned> {
    if let Some(helper) = helper {
        match start_helped(helper, &program, cwd, env, lifeline) {
            Ok(spawned) => return Ok(spawned),
            // The helper may be running the command (it did not answer, or
            // named a process that is not its shell): do not run it twice.
            Err(error) if matches!(error.kind(), ErrorKind::TimedOut | ErrorKind::InvalidData) => {
                return Err(error);
            }
            Err(error) => warn_once(helper, &error),
        }
    }
    let child = start(program, cwd, env, lifeline)?;
    Ok(Spawned {
        child,
        cleanup: None,
        helper: None,
    })
}

/// Start `program` under `helper` and wait for its first line, the shell's
/// pid, which must name a child of the helper that leads its own group (or
/// one that is already gone), so a helper cannot make this process signal
/// anything else. Fails with [`ErrorKind::UnexpectedEof`] when the helper
/// ended without starting the shell (it does not know its arguments, say),
/// with [`ErrorKind::TimedOut`] when it did not answer, and with
/// [`ErrorKind::InvalidData`] when the line is not such a pid: a helper that
/// might be running the command. A helper killed by a signal before the pid
/// was checked (the command killed its parent at once) did start the shell:
/// the shell is then found by the output pipes only it holds, and the
/// command is not run again.
fn start_helped(
    helper: &ReaperCommand,
    program: &Command,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Spawned> {
    let inner = program.as_std();
    let (link, theirs) = UnixStream::pair()?;
    let mut wrapped = Command::new(&helper.program);
    wrapped.args(&helper.args);
    if lifeline {
        wrapped.arg("--stdin");
    }
    wrapped
        .arg("--")
        .arg(inner.get_program())
        .args(inner.get_args());
    // A helper is stopped by its stdin closing, not killed outright.
    let mut child = launch(wrapped, cwd, env, Stdio::from(OwnedFd::from(theirs)), false)?;
    link.set_read_timeout(Some(HANDSHAKE))?;
    let shell = read_pid(&link).and_then(|pid| led_by(pid, child.id()));
    match shell {
        Ok(shell) => Ok(Spawned {
            child,
            cleanup: None,
            helper: Some(Helper { link, shell }),
        }),
        // Killed (by the command, say) before the shell could be checked: it
        // started, so it must neither run again nor be left running.
        Err(error)
            if matches!(
                error.kind(),
                ErrorKind::UnexpectedEof | ErrorKind::InvalidData
            ) && killed_by_signal(&mut child) =>
        {
            let shell = orphaned_shell(&child);
            Ok(Spawned {
                child,
                cleanup: None,
                helper: Some(Helper { link, shell }),
            })
        }
        Err(error) => {
            // Whatever answered, and did not end, is not the helper.
            if error.kind() != ErrorKind::UnexpectedEof {
                Group::of(child.id(), None).kill();
            }
            Err(error)
        }
    }
}

/// How long to wait for a helper that is being killed to be reaped.
const KILLED: Duration = Duration::from_millis(200);

/// Whether `child` ended by a signal, waiting briefly for one that is
/// being killed: only such a helper can have started the shell without
/// saying so.
fn killed_by_signal(child: &mut Child) -> bool {
    let until = std::time::Instant::now() + KILLED;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.signal().is_some(),
            Ok(None) if std::time::Instant::now() < until => {
                std::thread::sleep(Duration::from_millis(1));
            }
            _ => return false,
        }
    }
}

/// The shell of a helper that was killed before the server learned its
/// pid: the group leader that holds the pipes of `child`, which are unique to
/// this command. `None` when no such process runs (it ended already).
fn orphaned_shell(child: &Child) -> Option<u32> {
    let pipes: Vec<String> = [
        (child.stdout.as_ref()).map(|pipe| rustix::fs::fstat(pipe).map(|stat| stat.st_ino)),
        (child.stderr.as_ref()).map(|pipe| rustix::fs::fstat(pipe).map(|stat| stat.st_ino)),
    ]
    .into_iter()
    .flatten()
    .filter_map(Result::ok)
    .map(|inode| format!("pipe:[{inode}]"))
    .collect();
    let mut found: Option<(u64, u32)> = None;
    for entry in std::fs::read_dir("/proc").ok()?.flatten() {
        let Some(pid) = (entry.file_name().to_str()).and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let holds = std::fs::read_dir(entry.path().join("fd"))
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|fd| std::fs::read_link(fd.path()).ok())
            .any(|target| pipes.iter().any(|pipe| target.as_os_str() == pipe.as_str()));
        if !holds || pid == std::process::id() {
            continue;
        }
        // The shell leads its group; the earliest one started is the shell.
        if let Some(started) = leader_started(pid)
            && found.is_none_or(|(first, _)| started < first)
        {
            found = Some((started, pid));
        }
    }
    found.map(|(_, pid)| pid)
}

/// When `pid` started (clock ticks since boot) if it leads its own group.
fn leader_started(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // After the name: `state ppid pgrp session tty tpgid flags minflt
    // cminflt majflt cmajflt utime stime cutime cstime priority nice threads
    // itrealvalue starttime`.
    let rest: Vec<&str> = stat.rsplit_once(')')?.1.split_whitespace().collect();
    (rest.get(2)?.parse::<u32>().ok()? == pid)
        .then(|| rest.get(19)?.parse().ok())
        .flatten()
}

/// The pid on the helper's first line.
fn read_pid(mut link: &UnixStream) -> io::Result<u32> {
    let mut line = [0u8; 16];
    let read = match link.read(&mut line) {
        Ok(0) => return Err(ErrorKind::UnexpectedEof.into()),
        Ok(read) => read,
        Err(error) if error.kind() == ErrorKind::WouldBlock => {
            return Err(ErrorKind::TimedOut.into());
        }
        Err(error) => return Err(error),
    };
    let pid = std::str::from_utf8(&line[..read])
        .ok()
        .and_then(|text| text.trim().parse::<u32>().ok());
    pid.filter(|&pid| pid > 1)
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "the helper sent no pid"))
}

/// `pid` if it is a child of `parent` and leads its own process group, `None`
/// if it is gone already (nothing to signal), an error if it is anything
/// else: some process a helper must not make this one signal.
fn led_by(pid: u32, parent: Option<u32>) -> io::Result<Option<u32>> {
    let stat = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => stat,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    // `pid (comm) state ppid pgrp ...`; the name may hold spaces and `)`.
    let mut fields = stat
        .rsplit_once(')')
        .map(|(_, rest)| rest.split_whitespace());
    let ppid = fields.as_mut().and_then(|fields| fields.nth(1));
    let pgrp = fields.as_mut().and_then(Iterator::next);
    let theirs = |field: Option<&str>| field.and_then(|field| field.parse::<u32>().ok());
    match (theirs(ppid), theirs(pgrp)) {
        (Some(ppid), Some(pgrp)) if Some(ppid) == parent && pgrp == pid => Ok(Some(pid)),
        _ => Err(io::Error::new(
            ErrorKind::InvalidData,
            "the helper named a process that is not its shell",
        )),
    }
}

/// Say once per process that commands run without their helper.
fn warn_once(helper: &ReaperCommand, error: &io::Error) {
    static WARNED: Once = Once::new();
    WARNED.call_once(|| {
        tracing::warn!(
            helper = %helper.program.display(),
            %error,
            "the helper that ends a command's processes did not start; commands run without it"
        );
    });
}

/// Start `program` in `cwd` with exactly `env`, as the leader of a new
/// process group, stdout and stderr piped, stdin a pipe with `lifeline`
/// (the caller holds its write end) and closed without. Dropping the child
/// kills it. For a program that is not a shell the helper holds (the
/// `docker` client).
///
/// Every descriptor above stderr is first marked close-on-exec, so one
/// this process holds without it (inherited from its parent, or opened by
/// a library) does not pass to the command; Landlock checks only opens,
/// so a passed descriptor would reach past a sandbox.
pub(crate) fn start(
    program: Command,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    lifeline: bool,
) -> io::Result<Child> {
    let stdin = match lifeline {
        true => Stdio::piped(),
        false => Stdio::null(),
    };
    launch(program, cwd, env, stdin, true)
}

/// [`start`] and [`start_helped`]: `kill_on_drop` is whether dropping the
/// child kills it.
fn launch(
    mut program: Command,
    cwd: &Path,
    env: &BTreeMap<String, String>,
    stdin: Stdio,
    kill_on_drop: bool,
) -> io::Result<Child> {
    close_fds::set_fds_cloexec_threadsafe(3, &[]);
    program
        .stdin(stdin)
        .current_dir(cwd)
        .env_clear()
        .envs(env)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(kill_on_drop)
        .spawn()
}

/// Stop a command that was started and not supervised: its group is
/// killed, and a helper ends the rest when its socket closes with `spawned`.
pub(crate) fn abandon(spawned: Spawned) {
    let leader = match &spawned.helper {
        Some(helper) => helper.shell,
        None => spawned.child.id(),
    };
    Group::of(leader, None).kill();
    drop(spawned);
}

/// Pump the command's output into `on_out` and `on_err` until it exits,
/// times out or `cancel` fires, then stop it. A remote command is stopped
/// through its host, which must confirm (see [`RemoteRun::supervise`]).
pub(super) async fn supervise(
    started: Started,
    timeout: Duration,
    cancel: &CancellationToken,
    on_out: impl FnMut(&[u8]),
    on_err: impl FnMut(&[u8]),
) -> io::Result<End> {
    match started {
        Started::Process(spawned) => {
            supervise_process(spawned, timeout, cancel, on_out, on_err).await
        }
        Started::Remote(run) => Ok(run.supervise(timeout, cancel, on_out, on_err).await),
    }
}

/// Pump `child`'s output into `on_out` and `on_err` until it exits, times
/// out or `cancel` fires; then kill the shell's group and, under a helper,
/// close its socket so it ends what left the group, drain what is left,
/// reap the child and, unless it exited, run the cleanup. Dropping the
/// future stops the command too and starts the cleanup.
async fn supervise_process(
    spawned: Spawned,
    timeout: Duration,
    cancel: &CancellationToken,
    mut on_out: impl FnMut(&[u8]),
    mut on_err: impl FnMut(&[u8]),
) -> io::Result<End> {
    let Spawned {
        mut child,
        cleanup,
        helper,
    } = spawned;
    let leader = child.id();
    // The shell's group: the shell leads it under a helper, the child does
    // without. Declared after `child`, so on drop it kills the group before
    // the child handle goes.
    let shell = (helper.as_ref()).map_or(leader, |helper| helper.shell);
    let mut group = Group::of(shell, cleanup);
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        unreachable!("both streams are piped");
    };
    // The write ends the caller holds, out of `child`: `Child::wait` closes
    // the stdin it holds, which would stop the command at once. Closing them
    // stops it for the helper or the watchdog.
    let mut lifeline = child.stdin.take();
    let mut link = helper.map(|helper| helper.link);
    let deadline = Instant::now() + timeout;
    let end = {
        let mut pumps = pin!(async {
            tokio::join!(pump(stdout, &mut on_out), pump(stderr, &mut on_err));
        });
        let mut pumped = false;
        let end = loop {
            tokio::select! {
                biased;
                status = child.wait() => break End::Exited(status?.into()),
                () = cancel.cancelled() => break End::Aborted,
                () = tokio::time::sleep_until(deadline) => break End::TimedOut,
                () = &mut pumps, if !pumped => pumped = true,
            }
        };
        // The shell's group at once, as without a helper (and it covers a
        // helper that was killed), then the helper, which ends what had
        // left the group.
        group.kill();
        drop((lifeline.take(), link.take()));
        if !pumped {
            let _ = tokio::time::timeout(DRAIN, &mut pumps).await;
        }
        end
    };
    if !matches!(end, End::Exited(_)) {
        if tokio::time::timeout(REAP, child.wait()).await.is_err() {
            // The helper did not end (a process stuck in the kernel): fall
            // back to its group, which holds at least the helper.
            Group::of(leader, None).kill();
            let _ = tokio::time::timeout(REAP, child.wait()).await;
        }
        group.clean().await;
    }
    // The child is reaped (or left to tokio's reaper), so its id may be
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

    /// The server dying closes the lifeline's write end; the command and
    /// everything it started then end, with nobody signalling it. It needs
    /// nothing on the `PATH`.
    #[tokio::test]
    async fn closing_the_lifeline_kills_the_group() {
        let dir = tempfile::tempdir().unwrap();
        let env = BTreeMap::from([("PATH".to_owned(), "/nonexistent".to_owned())]);
        let Spawned {
            mut child, helper, ..
        } = spawn("/bin/sleep 30", dir.path(), &env, true).unwrap();
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

    fn sh(command: &str) -> Command {
        let mut shell = Command::new("/bin/sh");
        shell.args(["-c", command]);
        shell
    }

    fn command(program: &str) -> ReaperCommand {
        ReaperCommand {
            program: program.into(),
            args: Vec::new(),
        }
    }

    /// A helper that is not there, or that exits without starting the
    /// shell (an executable that does not know how it is called), is passed
    /// over: the command runs, as without a helper.
    #[tokio::test]
    async fn a_helper_that_does_not_run_the_command_is_passed_over() {
        let dir = tempfile::tempdir().unwrap();
        let env = BTreeMap::from([("PATH".to_owned(), "/usr/bin:/bin".to_owned())]);
        for helper in ["/nonexistent/linch-reaper", "/bin/true", "/bin/false"] {
            let helper = command(helper);
            let command = sh("echo ran");
            let spawned = start_shell(Some(&helper), command, dir.path(), &env, false).unwrap();
            assert!(spawned.helper.is_none(), "{helper:?}");
            let output = spawned.child.wait_with_output().await.unwrap();
            assert_eq!(output.stdout, b"ran\n", "{helper:?}");
        }
    }

    /// Kills the pids the command wrote to `pid` files when dropped, so a
    /// failed test leaves nothing behind.
    struct Pids(std::path::PathBuf);

    impl Drop for Pids {
        fn drop(&mut self) {
            if let Ok(text) = std::fs::read_to_string(self.0.join("shell"))
                && let Ok(pid) = text.trim().parse::<i32>()
                && let Some(pid) = Pid::from_raw(pid)
            {
                let _ = rustix::process::kill_process_group(pid, Signal::KILL);
                let _ = rustix::process::kill_process(pid, Signal::KILL);
            }
        }
    }

    /// Whether process `pid` runs (a zombie does not count).
    fn runs(pid: u32) -> bool {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) => !stat
                .rsplit_once(')')
                .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z')),
            Err(_) => false,
        }
    }

    /// A helper (a shell that starts the program with `setsid`, so it leads
    /// a group) that starts the program, waits until the program has written
    /// its `shell` file (the real helper starts the shell in its own group at
    /// once; `setsid` does so a moment after the fork), then writes `report`
    /// and kills itself: what a command that kills its parent at once does to
    /// the real helper, before or after the helper's first line.
    fn killed_helper(report: &str) -> ReaperCommand {
        let script =
            format!("setsid \"$@\" & until [ -s shell ]; do sleep 0.01; done; {report} kill -9 $$");
        ReaperCommand {
            program: "/bin/sh".into(),
            args: vec!["-c".into(), script.into()],
        }
    }

    /// The command ends with the call, and ran once, whichever side of
    /// the helper's first line the helper was killed on.
    async fn a_killed_helper_leaves_nothing(report: &str) {
        let dir = tempfile::tempdir().unwrap();
        let _pids = Pids(dir.path().to_path_buf());
        let env = BTreeMap::from([("PATH".to_owned(), "/usr/bin:/bin".to_owned())]);
        let command = sh("echo $$ > shell; echo run >> ran; exec sleep 20");
        let helper = killed_helper(report);
        let spawned = start_shell(Some(&helper), command, dir.path(), &env, false).unwrap();
        let shell = (spawned.helper.as_ref())
            .and_then(|helper| helper.shell)
            .expect("the shell is found though its helper was killed");
        let cancel = CancellationToken::new();
        let finished = run(spawned.into(), Duration::from_secs(10), &cancel)
            .await
            .unwrap();
        assert_eq!(finished.end, End::Exited(Exit::Signal(9)));
        let until = Instant::now() + Duration::from_secs(3);
        while runs(shell) && Instant::now() < until {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(!runs(shell), "the shell {shell} ran on");
        let ran = std::fs::read_to_string(dir.path().join("ran")).unwrap_or_default();
        assert!(ran.lines().count() <= 1, "the command ran twice: {ran:?}");
    }

    /// The helper is killed after it started the shell and before its first
    /// line: the call must not rerun the command without a helper, and the
    /// shell must end.
    #[tokio::test]
    async fn a_helper_killed_before_its_first_line_leaves_no_shell_and_no_rerun() {
        a_killed_helper_leaves_nothing("").await;
    }

    /// The helper is killed right after its first line, so the shell may no
    /// longer be its child when the server checks it.
    #[tokio::test]
    async fn a_helper_killed_right_after_its_first_line_leaves_no_shell() {
        a_killed_helper_leaves_nothing("echo $! >&0;").await;
    }

    /// The helper's first line must be a pid that can lead a group.
    #[test]
    fn a_pid_the_helper_sends_is_checked() {
        for (sent, ok) in [
            ("1234\n", true),
            ("0\n", false),
            ("x\n", false),
            ("1\n", false),
        ] {
            let (ours, mut theirs) = UnixStream::pair().unwrap();
            std::io::Write::write_all(&mut theirs, sent.as_bytes()).unwrap();
            assert_eq!(read_pid(&ours).is_ok(), ok, "{sent:?}");
        }
    }

    /// The pid must be a child of the helper that leads its own group; a
    /// process that is gone is not signalled; anything else is refused.
    #[test]
    fn a_pid_is_trusted_only_for_a_child_that_leads_its_group() {
        use std::os::unix::process::CommandExt;

        let me = std::process::id();
        let mut leader = std::process::Command::new("sleep");
        leader.arg("30").process_group(0);
        let mut leader = leader.spawn().unwrap();
        let mut member = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let (leads, member_pid) = (leader.id(), member.id());
        assert_eq!(led_by(leads, Some(me)).unwrap(), Some(leads));
        // Not the helper's child, or not leading a group of its own.
        assert!(led_by(leads, Some(me + 1)).is_err());
        assert!(led_by(member_pid, Some(me)).is_err());
        // Some other process of the user's: this one's parent.
        let parent = std::os::unix::process::parent_id();
        assert!(led_by(parent, Some(me)).is_err());
        let _ = (leader.kill(), member.kill());
        let _ = (leader.wait(), member.wait());
        assert_eq!(led_by(leads, Some(me)).unwrap(), None);
    }
}
