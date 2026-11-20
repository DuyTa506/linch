//! `linch-server --linch-reaper -- PROGRAM ARG...` runs the helper that ends
//! what a local command started: `linch-server` is its own helper, so the
//! wheel needs no second program.

#![cfg(target_os = "linux")]

use std::io::{BufRead, BufReader};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::process::{Command, Stdio};
use std::time::Duration;

/// The helper starts the program, reports its pid on its stdin socket, and
/// ends with the program's exit code.
#[test]
fn the_server_runs_as_the_helper_when_asked() {
    let (ours, theirs) = UnixStream::pair().unwrap();
    ours.set_read_timeout(Some(Duration::from_secs(20)))
        .unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_linch-server"));
    command
        .args(["--linch-reaper", "--", "/bin/sh", "-c", "echo $$; exit 3"])
        .stdin(Stdio::from(OwnedFd::from(theirs)))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().unwrap();
    drop(command);
    let mut line = String::new();
    BufReader::new(&ours).read_line(&mut line).unwrap();
    let reported: u32 = line.trim().parse().expect("a pid on the first line");
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(3));
    let printed = String::from_utf8_lossy(&output.stdout);
    assert_eq!(printed.trim(), reported.to_string());
}
