//! The file tools stay inside the workspace root while another process
//! that shares the workspace (a background `Bash` command) swaps a
//! directory or a file for a symlink between any two steps of a call.
//!
//! Every loop is bounded by a number of attempts, not by time.

mod common;

use std::fs;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use common::{Workspace, call};
use linch_core::SessionId;
use linch_tools::fs::{GlobQuery, GrepMode, GrepQuery};
use linch_tools::{Bash, ExecutionBackend, ExecutionConfig, ExecutionFs, Read, SandboxPolicy};
use serde_json::json;
use tokio_util::sync::CancellationToken;

const MAX_READ_BYTES: u64 = 10 * 1024 * 1024;
const ATTEMPTS: u32 = 20_000;

/// A thread that runs `swap` until it is dropped.
struct Swapper {
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Swapper {
    fn start(swap: impl Fn() + Send + 'static) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = std::thread::spawn(move || {
            while !flag.load(Ordering::Relaxed) {
                swap();
            }
        });
        Self {
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for Swapper {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

/// Swap `root/<name>` between a real directory and a symlink to `outside`,
/// as a background command in the workspace could.
fn swap_directory(root: PathBuf, name: &'static str, outside: PathBuf) -> impl Fn() + Send {
    let (d, kept) = (root.join(name), root.join(format!("{name}.real")));
    move || {
        let _ = fs::rename(&d, &kept);
        let _ = symlink(&outside, &d);
        std::thread::yield_now();
        if fs::remove_file(&d).is_err() {
            let _ = fs::remove_dir_all(&d);
        }
        let _ = fs::rename(&kept, &d);
        std::thread::yield_now();
    }
}

/// Swap `root/<name>` between a regular file and a symlink to `target`
/// with atomic renames, so the name never goes missing.
fn swap_file(root: PathBuf, name: &'static str, target: PathBuf) -> impl Fn() + Send {
    let (file, kept, link) = (root.join(name), root.join("f.real"), root.join("f.link"));
    move || {
        let _ = fs::rename(&file, &kept);
        let _ = symlink(&target, &link);
        let _ = fs::rename(&link, &file);
        std::thread::yield_now();
        let _ = fs::rename(&kept, &file);
        std::thread::yield_now();
    }
}

fn sandbox(ws: &Workspace) -> Option<ExecutionBackend> {
    let config = ExecutionConfig::Sandbox(SandboxPolicy::default());
    match ExecutionBackend::open(ws.fs.clone(), &config) {
        Ok(backend) => Some(backend),
        Err(error) => {
            eprintln!("skipped: {error}");
            None
        }
    }
}

fn read_text(result: linch_core::ToolResult) -> String {
    match result {
        Ok(output) => output.value.as_str().unwrap_or_default().to_owned(),
        Err(error) => error.message,
    }
}

#[tokio::test]
async fn a_write_stays_in_the_root_while_a_directory_is_swapped_for_a_symlink() {
    let ws = Workspace::new();
    fs::create_dir_all(ws.root().join("d")).unwrap();
    let _swapper = Swapper::start(swap_directory(ws.root(), "d", ws.outside()));
    let escaped = ws.outside().join("x.txt");
    for attempt in 0..ATTEMPTS {
        let _ = ws
            .fs
            .write("d/x.txt", b"from the model".to_vec(), None)
            .await;
        assert!(
            !escaped.exists(),
            "LocalFs::write created {} outside the root after {attempt} attempts",
            escaped.display()
        );
    }
}

#[tokio::test]
async fn a_read_stays_in_the_root_while_a_directory_is_swapped_for_a_symlink() {
    let ws = Workspace::new();
    ws.put("d/secret.txt", "inside");
    fs::write(ws.outside().join("secret.txt"), "HOST SECRET").unwrap();
    let _swapper = Swapper::start(swap_directory(ws.root(), "d", ws.outside()));
    for attempt in 0..ATTEMPTS {
        if let Ok(file) = ws.fs.read("d/secret.txt", MAX_READ_BYTES).await {
            assert_eq!(
                file.bytes, b"inside",
                "LocalFs::read returned a file from outside the root after {attempt} attempts"
            );
        }
    }
}

/// End to end, with the tools an agent has: a sandboxed background command
/// (which cannot read the host file itself) swaps the directory while the
/// `Read` tool, which runs in the server, reads through it.
#[tokio::test]
async fn a_sandboxed_command_cannot_make_read_return_a_host_file() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    ws.put("d/secret.txt", "inside");
    let secret = ws.outside().join("secret.txt");
    fs::write(&secret, "HOST SECRET").unwrap();
    let processes = Arc::new(linch_tools::Processes::new());
    let bash = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    let denied = read_text(
        call(
            &bash,
            json!({"command": format!("cat {}", secret.display())}),
        )
        .await,
    );
    assert!(denied.contains("Permission denied"), "{denied}");
    let swap = format!(
        "while :; do mv d d.real; ln -s {} d; rm d; mv d.real d; done",
        ws.outside().display()
    );
    call(&bash, json!({"command": swap, "run_in_background": true}))
        .await
        .unwrap();
    let read = Read::new(ws.fs.clone(), ws.reads.clone());
    let mut leaked = None;
    for _ in 0..ATTEMPTS {
        let text = read_text(call(&read, json!({"file_path": "d/secret.txt"})).await);
        if text.contains("HOST SECRET") {
            leaked = Some(text);
            break;
        }
    }
    processes.close_session(&SessionId::from("s1"));
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(leaked, None, "the Read tool returned a host file");
}

/// The file tools do not cover hard links; the sandbox does: Landlock
/// refuses `ln` from outside the workspace into it.
#[tokio::test]
async fn a_sandboxed_command_cannot_hard_link_a_host_file_into_the_workspace() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    let secret = ws.outside().join("secret.txt");
    fs::write(&secret, "HOST SECRET").unwrap();
    let bash = Bash::in_backend(&backend);
    let linked = read_text(
        call(
            &bash,
            json!({"command": format!("ln {} hard.txt", secret.display())}),
        )
        .await,
    );
    assert!(linked.contains("Invalid cross-device link"), "{linked}");
    assert!(!ws.root().join("hard.txt").exists());
}

#[tokio::test]
async fn a_read_and_a_write_stay_in_the_root_while_the_final_name_is_swapped_for_a_symlink() {
    let ws = Workspace::new();
    ws.put("f", "inside");
    let secret = ws.outside().join("secret.txt");
    fs::write(&secret, "HOST SECRET").unwrap();
    let _swapper = Swapper::start(swap_file(ws.root(), "f", secret.clone()));
    for attempt in 0..ATTEMPTS {
        if let Ok(file) = ws.fs.read("f", MAX_READ_BYTES).await {
            assert_ne!(
                file.bytes, b"HOST SECRET",
                "LocalFs::read followed the final name out of the root after {attempt} attempts"
            );
        }
        let _ = ws.fs.write("f", b"from the model".to_vec(), None).await;
        assert_eq!(
            fs::read(&secret).unwrap(),
            b"HOST SECRET",
            "LocalFs::write changed a file outside the root after {attempt} attempts"
        );
    }
}

#[tokio::test]
async fn a_nested_directory_swapped_for_a_symlink_is_not_followed() {
    let ws = Workspace::new();
    ws.put("a/b/secret.txt", "inside");
    fs::write(ws.outside().join("secret.txt"), "HOST SECRET").unwrap();
    let _swapper = Swapper::start(swap_directory(ws.root().join("a"), "b", ws.outside()));
    for attempt in 0..ATTEMPTS {
        if let Ok(file) = ws.fs.read("a/b/secret.txt", MAX_READ_BYTES).await {
            assert_eq!(
                file.bytes, b"inside",
                "read left the root through a/b after {attempt} attempts"
            );
        }
        let _ = ws.fs.write("a/b/x.txt", b"x".to_vec(), None).await;
        assert!(!ws.outside().join("x.txt").exists(), "write left the root");
    }
}

fn grep_query(pattern: &str) -> GrepQuery {
    GrepQuery {
        pattern: pattern.to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::Content,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 100,
    }
}

#[tokio::test]
async fn grep_and_glob_stay_in_the_root_while_a_directory_is_swapped_for_a_symlink() {
    let ws = Workspace::new();
    ws.put("d/secret.txt", "inside");
    ws.put("e/other.txt", "inside");
    fs::write(ws.outside().join("secret.txt"), "HOST SECRET").unwrap();
    fs::write(ws.outside().join("only-outside.txt"), "HOST SECRET").unwrap();
    let _swapper = Swapper::start(swap_directory(ws.root(), "d", ws.outside()));
    let cancel = CancellationToken::new();
    let glob = GlobQuery {
        pattern: "**/*.txt".to_owned(),
        path: None,
    };
    for attempt in 0..2_000 {
        let hits = ws
            .fs
            .grep(&grep_query("HOST SECRET"), &cancel)
            .await
            .unwrap();
        assert!(
            hits.files.is_empty(),
            "Grep read a file outside the root after {attempt} attempts"
        );
        assert!(
            !hits
                .skipped
                .iter()
                .any(|path| path.contains("only-outside")),
            "Grep named a file outside the root after {attempt} attempts: {:?}",
            hits.skipped
        );
        let paths: Vec<String> = ws
            .fs
            .glob(&glob, &cancel)
            .await
            .unwrap()
            .into_iter()
            .map(|hit| hit.path)
            .collect();
        assert!(
            !paths.iter().any(|path| path.ends_with("only-outside.txt")),
            "Glob listed a file outside the root after {attempt} attempts: {paths:?}"
        );
    }
}
