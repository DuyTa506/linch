//! The `linch-reaper` helper against real processes: what a command starts
//! and detaches from its process group must end with the command.
//!
//! Every test records the pids it expects to end and kills them by pid when
//! it finishes, even by a panic, so a failure leaves nothing behind.

#![cfg(target_os = "linux")]

use std::fs;
use std::io::{BufRead, BufReader};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::time::{Duration, Instant};

use rustix::process::{Pid, Signal, kill_process};
use tokio::process::Command;

/// Kills the pids it holds when dropped.
#[derive(Default)]
struct Pids(Vec<u32>);

impl Drop for Pids {
    fn drop(&mut self) {
        for &pid in &self.0 {
            if let Some(pid) = i32::try_from(pid).ok().and_then(Pid::from_raw) {
                let _ = kill_process(pid, Signal::KILL);
            }
        }
    }
}

/// Whether process `pid` runs (a zombie does not count).
fn alive(pid: u32) -> bool {
    match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => !stat
            .rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z')),
        Err(_) => false,
    }
}

/// The pid a command wrote to `dir/name`, waited for up to 10 s.
fn written(dir: &Path, name: &str) -> u32 {
    for _ in 0..500 {
        if let Ok(text) = fs::read_to_string(dir.join(name))
            && let Ok(pid) = text.trim().parse()
        {
            return pid;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("the command never wrote {name}");
}

/// Wait up to 3 s for `pid` to end.
async fn ends(pid: u32) -> bool {
    let until = Instant::now() + Duration::from_secs(3);
    while Instant::now() < until {
        if !alive(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

/// `linch-reaper -- program args...` in `dir`: stdin a pipe the caller
/// holds, the command's own group, as the server starts it.
fn reaper(program: &str, args: &[&str], dir: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_linch-reaper"));
    command
        .arg("--")
        .arg(program)
        .args(args)
        .current_dir(dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);
    command
}

fn shell(script: &str, dir: &Path) -> Command {
    reaper("/bin/sh", &["-c", script], dir)
}

/// Run `script` to its end and return how it exited, with the caller's end
/// of the pipe held throughout.
async fn run(script: &str, dir: &Path) -> ExitStatus {
    let mut child = shell(script, dir).spawn().unwrap();
    let _held = child.stdin.take();
    tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .expect("the command ended")
        .unwrap()
}

#[tokio::test]
async fn the_programs_exit_code_and_signal_pass_through() {
    let dir = tempfile::tempdir().unwrap();
    assert_eq!(run("exit 7", dir.path()).await.code(), Some(7));
    assert_eq!(run("exit 0", dir.path()).await.code(), Some(0));
    assert_eq!(run("kill -TERM $$", dir.path()).await.signal(), Some(15));
    assert_eq!(run("exec /nonexistent", dir.path()).await.code(), Some(127));
}

#[tokio::test]
async fn a_program_that_cannot_start_is_a_127_or_126() {
    let dir = tempfile::tempdir().unwrap();
    let mut missing = reaper("/nonexistent/sh", &[], dir.path());
    let mut child = missing.spawn().unwrap();
    let _held = child.stdin.take();
    assert_eq!(child.wait().await.unwrap().code(), Some(127));
}

/// The command leads its own process group, as `Bash` documents, and its
/// stdin is at end of file (not the caller's pipe).
#[tokio::test]
async fn the_program_leads_its_own_group_and_has_no_stdin() {
    let dir = tempfile::tempdir().unwrap();
    let leader = "[ \"$(ps -o pgid= -p $$ | tr -d ' ')\" = \"$$\" ] || exit 6; exit 0";
    assert_eq!(run(leader, dir.path()).await.code(), Some(0));
    let stdin = "if read -r _; then exit 5; fi; exit 0";
    assert_eq!(run(stdin, dir.path()).await.code(), Some(0));
}

/// Start `script` under the helper with stdin a socket: the caller's end
/// and the child. `extra` goes before the `--`.
fn on_a_socket(script: &str, extra: &[&str], dir: &Path) -> (UnixStream, tokio::process::Child) {
    let (ours, theirs) = UnixStream::pair().unwrap();
    ours.set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut helper = Command::new(env!("CARGO_BIN_EXE_linch-reaper"));
    helper
        .args(extra)
        .args(["--", "/bin/sh", "-c", script])
        .current_dir(dir)
        .stdin(Stdio::from(OwnedFd::from(theirs)))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);
    let child = helper.spawn().unwrap();
    (ours, child)
}

/// The helper writes the program's pid, one line, on its stdin: the server
/// signals the shell by it.
#[tokio::test]
async fn the_helper_reports_the_pid_of_the_program_on_stdin() {
    let dir = tempfile::tempdir().unwrap();
    let (ours, mut child) = on_a_socket("echo $$ > pid; exec sleep 3117", &[], dir.path());
    let mut line = String::new();
    BufReader::new(&ours).read_line(&mut line).unwrap();
    let reported: u32 = line.trim().parse().expect("a pid on the first line");
    let mut pids = Pids(vec![reported]);
    assert_eq!(reported, written(dir.path(), "pid"));
    assert!(alive(reported));
    drop(ours);
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .expect("the helper ended")
        .unwrap();
    assert_eq!(status.signal(), Some(9));
    assert!(ends(reported).await, "the program ran on");
    pids.0.clear();
}

/// `--stdin` passes the socket on to the program (for the lifeline
/// wrapper); without it the program has `/dev/null`.
#[tokio::test]
async fn the_program_has_the_socket_for_stdin_only_when_asked() {
    let dir = tempfile::tempdir().unwrap();
    for (extra, code) in [(&["--stdin"][..], 0), (&[][..], 1)] {
        let (ours, mut child) = on_a_socket("[ -S /dev/stdin ]", extra, dir.path());
        let status = tokio::time::timeout(Duration::from_secs(10), child.wait())
            .await
            .expect("the helper ended")
            .unwrap();
        assert_eq!(status.code(), Some(code), "{extra:?}");
        drop(ours);
    }
}

/// A `setsid` background process: the shell exits normally and the process
/// is handed to `init` unless something holds it.
#[tokio::test]
async fn a_setsid_process_ends_with_the_shell() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1 & until [ -s pid ]; do sleep 0.02; done; echo done";
    assert_eq!(run(script, dir.path()).await.code(), Some(0));
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ran on after the shell");
}

#[tokio::test]
async fn a_process_that_changes_its_group_ends_with_the_shell() {
    if !Path::new("/usr/bin/python3").exists() {
        return eprintln!("skipped: no python3");
    }
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "python3 -c \"import os,time; os.setpgid(0,0); open('pid','w').write(str(os.getpid())); time.sleep(3117)\" & until [ -s pid ]; do sleep 0.02; done";
    assert_eq!(run(script, dir.path()).await.code(), Some(0));
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ran on after the shell");
}

/// The classic daemon: fork, `setsid`, fork again, parents exit.
#[tokio::test]
async fn a_double_forked_daemon_ends_with_the_shell() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "(setsid sh -c 'sh -c \"echo \\$\\$ > pid; exec sleep 3117\" &') >/dev/null 2>&1; until [ -s pid ]; do sleep 0.02; done";
    assert_eq!(run(script, dir.path()).await.code(), Some(0));
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "daemon {pid} ran on after the shell");
}

#[tokio::test]
async fn a_process_that_ignores_sigterm_still_ends() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "setsid sh -c \"trap '' TERM HUP INT; echo \\$\\$ > pid; while :; do sleep 1; done\" >/dev/null 2>&1 & until [ -s pid ]; do sleep 0.02; done";
    assert_eq!(run(script, dir.path()).await.code(), Some(0));
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ignored the kill");
}

/// The whole tree is found: a grandchild whose parent also left the group.
#[tokio::test]
async fn a_tree_of_escapees_ends_with_the_shell() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "setsid sh -c 'setsid sh -c \"echo \\$\\$ > pid; exec sleep 3117\" & sleep 3117' >/dev/null 2>&1 & until [ -s pid ]; do sleep 0.02; done";
    assert_eq!(run(script, dir.path()).await.code(), Some(0));
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ran on after the shell");
}

/// Closing the pipe (the caller stopping the command, or dying) ends a
/// running command and everything it detached.
#[tokio::test]
async fn closing_the_pipe_ends_the_command_and_what_it_detached() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1 & sleep 3117";
    let mut child = shell(script, dir.path()).spawn().unwrap();
    let held = child.stdin.take();
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    let running = tokio::time::timeout(Duration::from_millis(300), child.wait()).await;
    assert!(running.is_err(), "the command runs while the pipe is held");
    assert!(alive(pid));
    drop(held);
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .expect("the helper ended")
        .unwrap();
    assert_eq!(status.signal(), Some(9));
    assert!(
        ends(pid).await,
        "process {pid} ran on after the pipe closed"
    );
}

/// Dropping the child handle closes its stdin, which stops the command.
#[tokio::test]
async fn dropping_the_child_ends_what_it_detached() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let script = "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1 & sleep 3117";
    let child = shell(script, dir.path()).spawn().unwrap();
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    drop(child);
    assert!(
        ends(pid).await,
        "process {pid} ran on after the handle dropped"
    );
}

/// The pipe's other end held by another process, killed with `SIGKILL`:
/// what the server dying looks like.
#[tokio::test]
async fn the_pipes_holder_dying_ends_the_command_and_what_it_detached() {
    let dir = tempfile::tempdir().unwrap();
    let mut pids = Pids::default();
    let (reader, writer) = std::io::pipe().unwrap();
    let mut spawn_holder = std::process::Command::new("/bin/sh");
    spawn_holder
        .args(["-c", "exec sleep 3117"])
        .stdin(Stdio::from(writer));
    let mut holder = spawn_holder.spawn().unwrap();
    // The write end was in this command too; only the holder may keep it.
    drop(spawn_holder);
    pids.0.push(holder.id());
    let script = "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1 & sleep 3117";
    let mut helper = shell(script, dir.path());
    helper.stdin(Stdio::from(reader));
    let mut child = helper.spawn().unwrap();
    let pid = written(dir.path(), "pid");
    pids.0.push(pid);
    assert!(alive(pid));
    holder.kill().unwrap();
    holder.wait().unwrap();
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .expect("the helper ended")
        .unwrap();
    assert_eq!(status.signal(), Some(9));
    assert!(
        ends(pid).await,
        "process {pid} ran on after the holder died"
    );
}
