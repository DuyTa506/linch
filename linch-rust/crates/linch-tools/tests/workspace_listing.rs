//! What `Grep`, `Glob` and the path checks tell about names, while a racing
//! process swaps directories, and for entries a search cannot read.
//!
//! The racers are `python3` processes that exchange two names with
//! `renameat2(RENAME_EXCHANGE)`, so a name is never missing: it is a
//! directory in one instant and a symlink in the next. Every loop is bounded
//! by attempts.

mod common;

use std::fs;
use std::os::unix::fs::{PermissionsExt as _, symlink};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::Duration;

use common::{Workspace, call};
use linch_core::{SessionId, ToolResult};
use linch_tools::fs::{ExecutionFs, GrepMode, GrepQuery};
use linch_tools::{Bash, ExecutionBackend, ExecutionConfig, Glob, Grep, Processes, SandboxPolicy};
use serde_json::json;
use tokio_util::sync::CancellationToken;

const ATTEMPTS: u32 = 20_000;
const HOST_NAME: &str = "only-outside";

const EXCHANGE: &str = r#"
import ctypes, sys
libc = ctypes.CDLL(None, use_errno=True)
a, b = sys.argv[1], sys.argv[2]
while True:
    libc.renameat2(-100, a.encode(), -100, b.encode(), 2)
"#;

/// Racing processes, killed on drop.
struct Racers(Vec<Child>);

impl Racers {
    fn exchange(dir: &Path, copies: usize, a: &str, b: &str) -> Self {
        let script = dir.join(".racer.py");
        fs::write(&script, EXCHANGE).unwrap();
        let children = (0..copies)
            .map(|_| {
                Command::new("python3")
                    .arg(&script)
                    .args([a, b])
                    .current_dir(dir)
                    .stdin(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                    .expect("python3 is needed for the racers")
            })
            .collect();
        std::thread::sleep(Duration::from_millis(150));
        Self(children)
    }
}

impl Drop for Racers {
    fn drop(&mut self) {
        for child in &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn any(result: ToolResult) -> String {
    match result {
        Ok(output) => output.value.as_str().unwrap_or_default().to_owned(),
        Err(error) => error.message,
    }
}

/// A host directory with names a search could be steered to list.
fn host_files(ws: &Workspace) {
    for n in 0..6 {
        fs::write(ws.outside().join(format!("{HOST_NAME}-key{n}")), "k").unwrap();
    }
}

fn names_query() -> GrepQuery {
    GrepQuery {
        pattern: "x".to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 1000,
    }
}

/// A directory that is readable but not searchable, exchanged with a
/// symlink to a host directory: the walk may list the host directory, and
/// the re-open from the root finds the real directory, where every name
/// fails. No such name may be reported.
#[tokio::test]
async fn grep_does_not_name_host_files_when_a_directory_is_swapped_for_a_symlink() {
    let ws = Workspace::new();
    host_files(&ws);
    fs::create_dir_all(ws.root().join("d")).unwrap();
    fs::set_permissions(ws.root().join("d"), fs::Permissions::from_mode(0o400)).unwrap();
    symlink(ws.outside(), ws.root().join("d.link")).unwrap();
    let racers = Racers::exchange(&ws.root(), 2, "d", "d.link");
    let cancel = CancellationToken::new();
    let mut leaked = None;
    for attempt in 0..ATTEMPTS {
        let hits = ws.fs.grep(&names_query(), &cancel).await.unwrap();
        if hits.skipped.iter().any(|path| path.contains(HOST_NAME)) {
            leaked = Some((attempt, hits.skipped));
            break;
        }
    }
    drop(racers);
    let _ = fs::set_permissions(ws.root().join("d"), fs::Permissions::from_mode(0o755));
    assert_eq!(leaked, None, "Grep named host files");
}

/// The same with the sandboxed `Bash` as the racer and the `Grep` tool's
/// text as the channel.
#[tokio::test]
async fn a_sandboxed_command_cannot_make_grep_name_host_files() {
    let ws = Workspace::new();
    let config = ExecutionConfig::Sandbox(SandboxPolicy::default());
    let Ok(backend) = ExecutionBackend::open(ws.fs.clone(), &config) else {
        eprintln!("skipped: no sandbox here");
        return;
    };
    host_files(&ws);
    ws.put("racer.py", EXCHANGE);
    let outside = ws.outside().display().to_string();
    let processes = Arc::new(Processes::new());
    let bash = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    let denied = any(call(&bash, json!({"command": format!("ls {outside}")})).await);
    assert!(denied.contains("Permission denied"), "{denied}");
    let script = format!(
        "mkdir d; chmod 400 d; ln -s {outside} d.link; \
         python3 racer.py d d.link & python3 racer.py d d.link & wait"
    );
    call(&bash, json!({"command": script, "run_in_background": true}))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(500)).await;
    let grep = Grep::new(ws.fs.clone());
    let mut leaked = None;
    for attempt in 0..ATTEMPTS {
        let text = any(call(&grep, json!({"pattern": "x"})).await);
        if text.contains(HOST_NAME) {
            leaked = Some((attempt, text));
            break;
        }
    }
    processes.close_session(&SessionId::from("s1"));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let _ = fs::set_permissions(ws.root().join("d"), fs::Permissions::from_mode(0o755));
    assert_eq!(leaked, None, "the Grep tool named host files");
}

/// Entries a search lists but cannot open are not named: a note counts them.
#[tokio::test]
async fn entries_a_search_cannot_read_are_counted_and_not_named() {
    let ws = Workspace::new();
    ws.put("top.txt", "needle\n");
    ws.put("locked/secret-name.txt", "needle\n");
    ws.put("locked/other-name.txt", "needle\n");
    fs::set_permissions(ws.root().join("locked"), fs::Permissions::from_mode(0o400)).unwrap();
    let grep = any(call(&Grep::new(ws.fs.clone()), json!({"pattern": "needle"})).await);
    let glob = any(call(&Glob::new(ws.fs.clone()), json!({"pattern": "**/*.txt"})).await);
    fs::set_permissions(ws.root().join("locked"), fs::Permissions::from_mode(0o755)).unwrap();
    for text in [grep, glob] {
        assert!(text.starts_with("top.txt"), "{text}");
        assert!(
            text.contains("2 entries could not be read and were left out"),
            "{text}"
        );
        assert!(!text.contains("-name"), "{text}");
    }
}

/// A file below 128 directories is searched like any other: a search holds
/// no descriptor per level, so it has no depth limit.
#[tokio::test]
async fn a_search_finds_a_file_below_128_directories() {
    let ws = Workspace::new();
    let deep = vec!["a"; 130].join("/");
    ws.put(&format!("{deep}/f.txt"), "needle\n");
    ws.put("top.txt", "needle\n");
    let grep = any(call(&Grep::new(ws.fs.clone()), json!({"pattern": "needle"})).await);
    assert!(
        grep.lines().any(|line| line == format!("{deep}/f.txt")),
        "{grep}"
    );
    assert!(!grep.contains("left out"), "{grep}");
    let glob = any(call(&Glob::new(ws.fs.clone()), json!({"pattern": "**/f.txt"})).await);
    assert_eq!(glob, format!("{deep}/f.txt"));
}
