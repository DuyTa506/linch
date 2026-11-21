//! The helper that runs a local command and ends everything it started
//! (Linux).
//!
//! `[--stdin] -- PROGRAM [ARG...]` starts `PROGRAM` as the leader of its own
//! process group, with the environment, working directory, stdout and stderr
//! the helper was started with and `/dev/null` for stdin (the helper's own
//! stdin with `--stdin`, for a program that watches it), and waits for it. A
//! process group cannot hold a command: a descendant that calls `setsid` or
//! `setpgid` leaves it, and one that double-forks is handed to `init` when
//! its parent exits. The helper is a child *subreaper*
//! (`PR_SET_CHILD_SUBREAPER`): every orphaned descendant is handed to it
//! instead, and it can list its children, so it can kill what is left.
//!
//! Its stdin is a socket the caller holds the other end of. It writes the
//! program's pid there as one line, right after starting it, so the caller
//! can signal the program itself. It ends when the program exits (with its
//! exit code, or by its signal) or when the caller's end closes (the caller
//! stopped the command, or died, even by `SIGKILL`; the helper then ends by
//! `SIGKILL`). Either way it first kills the program's group, then its own
//! children, round after round and reaping as it goes, until none is left.
//!
//! The helper is the `linch-reaper` program (built from this crate), or the
//! host's own executable run in this mode: a host calls [`enter`] first in
//! `main`, which runs the helper when the process was started as one and
//! otherwise offers the executable as the helper for the commands it starts.
//! Using it is opt-in, because it costs a process per command:
//! `LINCH_REAPER=1` turns on the host's helper, `LINCH_REAPER=/path` names
//! another, and a host that calls [`register`] turns on its own. A command is
//! held by its process group alone where there is no helper (see `Bash`). The
//! helper holds no descendant of a process it cannot reach (one started
//! through another service: `systemd-run`, `at`, `cron`, a D-Bus activation)
//! and can be killed by the command it supervises, which runs as the same
//! user; the server then still kills the shell's group, but not what had
//! left it. A helper killed before the server has checked the shell's pid
//! is not passed over (the shell may run): the server finds the shell by
//! the output pipes only it holds. A kernel without
//! `/proc/<pid>/task/<tid>/children` (`CONFIG_PROC_CHILDREN`) or
//! `pidfd_open` (before 5.3) can only have the program's group killed.

use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::OnceLock;

/// The first argument that makes a host executable run as the helper.
pub const ARG: &str = "--linch-reaper";

/// How to run the helper: the program, then these arguments, then
/// `-- PROGRAM [ARG...]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReaperCommand {
    /// The executable to start.
    pub program: PathBuf,
    /// The arguments before `--` (for a host executable, [`ARG`]).
    pub args: Vec<OsString>,
}

/// The helper a host named, and whether the host turned it on.
struct Host {
    command: ReaperCommand,
    on: bool,
}

/// What the host named, once.
static HOST: OnceLock<Host> = OnceLock::new();

/// The command the host named, and whether it is on without `LINCH_REAPER`.
pub(crate) fn host() -> Option<(&'static ReaperCommand, bool)> {
    HOST.get().map(|host| (&host.command, host.on))
}

/// Turn on `command` as the helper for the commands this process starts,
/// before the first one starts. The choice is process-wide because it names
/// the host's own executable, not anything one agent owns; the first one
/// wins.
///
/// Args:
///     command: How to start the helper.
///
/// Returns:
///     `Err` with `command` back when the host named one already.
pub fn register(command: ReaperCommand) -> Result<(), ReaperCommand> {
    HOST.set(Host { command, on: true })
        .map_err(|host| host.command)
}

/// For the first line of a host's `main`: when this process was started as
/// the helper (its first argument is [`ARG`]), run it and return the exit
/// code to end with; otherwise offer this executable as the helper, which
/// `LINCH_REAPER=1` turns on, and return `None`.
///
/// Returns:
///     The exit code of the helper, or `None` to go on as the host.
pub fn enter() -> Option<ExitCode> {
    #[cfg(target_os = "linux")]
    {
        let mut args = std::env::args_os().skip(1);
        if args.next().is_some_and(|arg| arg == ARG) {
            return Some(run(args));
        }
        if let Ok(program) = std::env::current_exe() {
            let command = ReaperCommand {
                program,
                args: vec![ARG.into()],
            };
            // A host that named its own helper first keeps it.
            let _ = HOST.set(Host { command, on: false });
        }
    }
    None
}

/// Run the helper for `args`, which are `-- PROGRAM [ARG...]`.
///
/// Returns:
///     The exit code to end with: the program's, or 2 for a bad command line,
///     127 or 126 when the program cannot start.
pub fn run(args: impl IntoIterator<Item = OsString>) -> ExitCode {
    #[cfg(target_os = "linux")]
    {
        linux::main(args.into_iter())
    }
    #[cfg(not(target_os = "linux"))]
    {
        drop(args);
        eprintln!("linch-reaper runs on Linux only");
        ExitCode::from(2)
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use std::ffi::OsString;
    use std::io::{self, ErrorKind};
    use std::os::fd::AsFd;
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    use std::process::{Child, Command, ExitCode, ExitStatus, Stdio};
    use std::time::Duration;
    use std::{fs, thread};

    use rustix::event::{PollFd, PollFlags, poll};
    use rustix::io::Errno;
    use rustix::process::{
        Pid, PidfdFlags, Signal, WaitOptions, getpid, kill_process, kill_process_group, pidfd_open,
        set_child_subreaper, wait,
    };

    /// Most rounds of listing and killing the children left, each after a
    /// pause: a tree that keeps forking is given up on after about two
    /// seconds.
    const ROUNDS: u32 = 2000;
    const PAUSE: Duration = Duration::from_millis(1);

    pub(super) fn main(mut args: impl Iterator<Item = OsString>) -> ExitCode {
        let usage = || {
            eprintln!("usage: linch-reaper [--stdin] -- PROGRAM [ARG...]");
            ExitCode::from(2)
        };
        let mut next = args.next();
        let stdin = next.as_deref().is_some_and(|arg| arg == "--stdin");
        if stdin {
            next = args.next();
        }
        if next.is_none_or(|arg| arg != "--") {
            return usage();
        }
        let Some(program) = args.next() else {
            return usage();
        };
        if let Err(error) = set_child_subreaper(Some(getpid())) {
            // Without it the program still runs, held by its group only.
            eprintln!("linch-reaper: cannot hold the command's processes: {error}");
        }
        let mut command = Command::new(program);
        command.args(args).process_group(0);
        command.stdin(match stdin {
            true => Stdio::inherit(),
            false => Stdio::null(),
        });
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                eprintln!("linch-reaper: cannot start the command: {error}");
                return ExitCode::from(match error.kind() {
                    ErrorKind::NotFound => 127,
                    _ => 126,
                });
            }
        };
        report(child.id());
        let (status, stopped) = wait_for(&mut child);
        sweep(child.id());
        finish(status, stopped)
    }

    /// Tell the caller the program's pid, one line on stdin. A caller that
    /// is gone (or a stdin that cannot be written) is found by `wait_for`.
    fn report(pid: u32) {
        let _ = rustix::io::write(io::stdin().as_fd(), format!("{pid}\n").as_bytes());
    }

    /// Wait until the program exits or the caller's end closes. The status
    /// is the program's when it exited first; `true` when the caller's end
    /// closed first.
    fn wait_for(child: &mut Child) -> (Option<ExitStatus>, bool) {
        let pidfd = i32::try_from(child.id())
            .ok()
            .and_then(Pid::from_raw)
            .and_then(|pid| pidfd_open(pid, PidfdFlags::empty()).ok());
        let Some(pidfd) = pidfd else {
            // No pidfd: wait for the program alone; the caller's end is not
            // watched, so stopping it is the caller's group kill.
            return (child.wait().ok(), false);
        };
        let stdin = io::stdin();
        loop {
            let mut fds = [
                PollFd::new(&pidfd, PollFlags::IN),
                PollFd::new(&stdin, PollFlags::empty()),
            ];
            match poll(&mut fds, None) {
                Ok(_) => {}
                Err(Errno::INTR) => continue,
                Err(_) => return (None, true),
            }
            if !fds[0].revents().is_empty() {
                return (child.wait().ok(), false);
            }
            if !fds[1].revents().is_empty() {
                return (None, true);
            }
        }
    }

    /// Kill what the program left: its group at once (no member can fork
    /// once it is signalled), then every child of this process, round after
    /// round, for what left the group, until none is left. Reaps as it goes.
    fn sweep(program: u32) {
        let group = i32::try_from(program).ok().and_then(Pid::from_raw);
        if let Some(group) = group {
            let _ = kill_process_group(group, Signal::KILL);
        }
        for _ in 0..ROUNDS {
            while let Ok(Some(_)) = wait(WaitOptions::NOHANG) {}
            let Some(children) = children() else {
                return;
            };
            if children.is_empty() {
                return;
            }
            for pid in children {
                let _ = kill_process(pid, Signal::KILL);
            }
            thread::sleep(PAUSE);
        }
    }

    /// This process's children; `None` where the kernel does not list them.
    fn children() -> Option<Vec<Pid>> {
        let text = fs::read_to_string("/proc/thread-self/children").ok()?;
        Some(
            (text.split_whitespace())
                .filter_map(|pid| Pid::from_raw(pid.parse().ok()?))
                .collect(),
        )
    }

    /// End like the program did (its exit code, or its signal), or, when the
    /// caller's end closed first, by `SIGKILL`, as a group kill would.
    fn finish(status: Option<ExitStatus>, stopped: bool) -> ExitCode {
        if let (false, Some(status)) = (stopped, status) {
            if let Some(code) = status.code() {
                return ExitCode::from(u8::try_from(code & 0xff).unwrap_or(1));
            }
            if let Some(signal) = status.signal() {
                if let Some(named) = Signal::from_named_raw(signal) {
                    let _ = kill_process(getpid(), named);
                }
                return ExitCode::from(u8::try_from((128 + signal) & 0xff).unwrap_or(1));
            }
        }
        let _ = kill_process(getpid(), Signal::KILL);
        ExitCode::from(137)
    }
}
