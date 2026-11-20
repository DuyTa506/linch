//! The memory one `Edit` of a file at the read limit may hold. This file
//! holds one test: the peak resident memory is a property of the whole
//! process, so another test running beside it would be counted.

mod common;

use common::{Workspace, call, text};
use linch_tools::{Edit, Read};
use serde_json::json;

const MIB: u64 = 1024 * 1024;

/// Peak resident memory of this process so far, in bytes.
fn peak_rss() -> u64 {
    let status = std::fs::read_to_string("/proc/self/status").unwrap();
    let line = status
        .lines()
        .find(|line| line.starts_with("VmHWM:"))
        .unwrap();
    let kb: u64 = line.split_whitespace().nth(1).unwrap().parse().unwrap();
    kb * 1024
}

/// A one-byte `old_string` over a 10 MiB file matches ten million times: the
/// edit must be sized and checked without a list of its matches.
#[tokio::test]
async fn edit_replace_all_does_not_hold_many_times_the_file() {
    let ws = Workspace::new();
    let read = Read::new(ws.fs.clone(), ws.reads.clone());
    let edit = Edit::new(ws.fs.clone(), ws.reads.clone());
    // Two small calls reach the limit: 4096 bytes, then `a` -> 2560 `a`s.
    ws.put("a.txt", &"a".repeat(4096));
    call(&read, json!({"file_path": "a.txt"})).await.unwrap();
    let grow = json!({
        "file_path": "a.txt", "old_string": "a", "new_string": "a".repeat(2560),
        "replace_all": true,
    });
    assert_eq!(
        text(call(&edit, grow).await),
        "Edited a.txt (4096 replacements)"
    );
    assert_eq!(ws.get("a.txt").len() as u64, 10 * MIB);

    let before = peak_rss();
    let shrink = json!({
        "file_path": "a.txt", "old_string": "a", "new_string": "", "replace_all": true,
    });
    let said = text(call(&edit, shrink).await);
    let grown = peak_rss().saturating_sub(before);
    assert_eq!(said, "Edited a.txt (10485760 replacements)");
    assert!(
        grown < 64 * MIB,
        "one Edit of a 10 MiB file raised the peak memory by {} MiB",
        grown / MIB
    );
}
