//! `Read`, `Write` and `Edit` on a temp workspace, and the read-before-write
//! policy they share.

mod common;

use std::fs;
use std::os::unix::fs::symlink;
use std::sync::Arc;

use common::{Workspace, call, code, ctx, input, text};
use linch_core::{Tool, ToolScope};
use linch_tools::{Edit, Read, Write};
use serde_json::json;

struct Tools {
    ws: Workspace,
    read: Read,
    write: Write,
    edit: Edit,
}

fn tools() -> Tools {
    let ws = Workspace::new();
    Tools {
        read: Read::new(ws.fs.clone(), ws.reads.clone()),
        write: Write::new(ws.fs.clone(), ws.reads.clone()),
        edit: Edit::new(ws.fs.clone(), ws.reads.clone()),
        ws,
    }
}

#[test]
fn scopes_are_explicit() {
    let t = tools();
    assert_eq!(t.read.scope(), ToolScope::Read);
    assert_eq!(t.write.scope(), ToolScope::Write);
    assert_eq!(t.edit.scope(), ToolScope::Write);
    assert_eq!(
        t.read.summarize(&input(json!({"file_path": "a"}))),
        "Read a"
    );
}

#[tokio::test]
async fn read_numbers_lines_and_windows() {
    let t = tools();
    t.ws.put("a.txt", "one\ntwo\nthree\n");
    let all = text(call(&t.read, json!({"file_path": "a.txt"})).await);
    assert_eq!(all, "1\tone\n2\ttwo\n3\tthree");
    let window = text(
        call(
            &t.read,
            json!({"file_path": "a.txt", "offset": 2, "limit": 1}),
        )
        .await,
    );
    assert_eq!(window, "2\ttwo");
    let past = text(call(&t.read, json!({"file_path": "a.txt", "offset": 9})).await);
    assert_eq!(past, "<no lines at offset 9; a.txt has 3 lines>");
    t.ws.put("empty.txt", "");
    let empty = text(call(&t.read, json!({"file_path": "empty.txt"})).await);
    assert_eq!(empty, "<file is empty>");
}

#[tokio::test]
async fn read_refuses_binary_non_text_missing_and_escaping_files() {
    let t = tools();
    t.ws.put("bin", "a\0b");
    assert_eq!(
        code(call(&t.read, json!({"file_path": "bin"})).await),
        "binary"
    );
    fs::write(t.ws.root().join("latin1"), [0xe9u8, 0x0a]).unwrap();
    assert_eq!(
        code(call(&t.read, json!({"file_path": "latin1"})).await),
        "not_text"
    );
    assert_eq!(
        code(call(&t.read, json!({"file_path": "nope"})).await),
        "not_found"
    );
    fs::write(t.ws.outside().join("secret"), "s").unwrap();
    let escape = call(&t.read, json!({"file_path": "../outside/secret"})).await;
    assert_eq!(code(escape), "path_escape");
    symlink(t.ws.outside().join("secret"), t.ws.root().join("link")).unwrap();
    assert_eq!(
        code(call(&t.read, json!({"file_path": "link"})).await),
        "path_escape"
    );
}

#[tokio::test]
async fn write_creates_new_files_without_a_read() {
    let t = tools();
    let out = text(
        call(
            &t.write,
            json!({"file_path": "deep/new.txt", "content": "hi"}),
        )
        .await,
    );
    assert_eq!(out, "Wrote deep/new.txt");
    assert_eq!(t.ws.get("deep/new.txt"), "hi");
    // Its own write counts as a whole read.
    let again = call(
        &t.write,
        json!({"file_path": "deep/new.txt", "content": "again"}),
    )
    .await;
    assert_eq!(text(again), "Wrote deep/new.txt");
}

#[tokio::test]
async fn write_over_an_existing_file_needs_a_whole_current_read() {
    let t = tools();
    t.ws.put("a.txt", "one\ntwo\n");
    let blind = call(&t.write, json!({"file_path": "a.txt", "content": "x"})).await;
    assert_eq!(code(blind), "read_before_write");
    call(&t.read, json!({"file_path": "a.txt", "limit": 1}))
        .await
        .unwrap();
    let partial = call(&t.write, json!({"file_path": "a.txt", "content": "x"})).await;
    assert_eq!(code(partial), "read_before_write");
    call(&t.read, json!({"file_path": "a.txt"})).await.unwrap();
    // Someone else changes the file after the read.
    t.ws.put("a.txt", "changed elsewhere\n");
    let stale = call(&t.write, json!({"file_path": "a.txt", "content": "x"})).await;
    let error = stale.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("read_before_write"));
    assert!(
        error.message.contains("changed since it was last read"),
        "{}",
        error.message
    );
    call(&t.read, json!({"file_path": "a.txt"})).await.unwrap();
    call(&t.write, json!({"file_path": "a.txt", "content": "x"}))
        .await
        .unwrap();
    assert_eq!(t.ws.get("a.txt"), "x");
}

#[tokio::test]
async fn evidence_belongs_to_one_session() {
    let t = tools();
    t.ws.put("a.txt", "one\n");
    let read = t
        .read
        .execute(input(json!({"file_path": "a.txt"})), ctx("s1"))
        .await;
    read.unwrap();
    let other = t
        .write
        .execute(
            input(json!({"file_path": "a.txt", "content": "x"})),
            ctx("s2"),
        )
        .await;
    assert_eq!(code(other), "read_before_write");
    t.ws.reads.forget(&"s1".into());
    let forgotten = call(&t.write, json!({"file_path": "a.txt", "content": "x"})).await;
    assert_eq!(code(forgotten), "read_before_write");
}

#[tokio::test]
async fn a_read_through_a_symlink_counts_for_the_real_file() {
    let t = tools();
    t.ws.put("real.txt", "one\n");
    symlink(t.ws.root().join("real.txt"), t.ws.root().join("alias.txt")).unwrap();
    call(&t.read, json!({"file_path": "alias.txt"}))
        .await
        .unwrap();
    let out = call(
        &t.write,
        json!({"file_path": "real.txt", "content": "two\n"}),
    )
    .await;
    assert_eq!(text(out), "Wrote real.txt");
    // The link itself is untouched: the write replaced the real file.
    assert!(
        fs::symlink_metadata(t.ws.root().join("alias.txt"))
            .unwrap()
            .is_symlink()
    );
    assert_eq!(t.ws.get("alias.txt"), "two\n");
}

#[tokio::test]
async fn edit_needs_a_read_and_replaces_one_exact_match() {
    let t = tools();
    t.ws.put("a.rs", "fn a() {}\nfn b() {}\n");
    let edit = json!({"file_path": "a.rs", "old_string": "fn b", "new_string": "fn c"});
    assert_eq!(code(call(&t.edit, edit.clone()).await), "read_before_write");
    call(&t.read, json!({"file_path": "a.rs"})).await.unwrap();
    assert_eq!(text(call(&t.edit, edit).await), "Edited a.rs");
    assert_eq!(t.ws.get("a.rs"), "fn a() {}\nfn c() {}\n");
    // The edit's own result is current, so a second edit needs no new read.
    let second = json!({"file_path": "a.rs", "old_string": "fn a", "new_string": "fn z"});
    assert_eq!(text(call(&t.edit, second).await), "Edited a.rs");
}

#[tokio::test]
async fn edit_reports_missing_ambiguous_and_replace_all() {
    let t = tools();
    t.ws.put("a.txt", "x y x\n");
    call(&t.read, json!({"file_path": "a.txt"})).await.unwrap();
    let missing = json!({"file_path": "a.txt", "old_string": "zzz", "new_string": "q"});
    assert_eq!(code(call(&t.edit, missing).await), "no_match");
    let twice = json!({"file_path": "a.txt", "old_string": "x", "new_string": "q"});
    assert_eq!(code(call(&t.edit, twice).await), "ambiguous_match");
    let all =
        json!({"file_path": "a.txt", "old_string": "x", "new_string": "q", "replace_all": true});
    assert_eq!(
        text(call(&t.edit, all).await),
        "Edited a.txt (2 replacements)"
    );
    assert_eq!(t.ws.get("a.txt"), "q y q\n");
    let empty = json!({"file_path": "a.txt", "old_string": "", "new_string": "q"});
    assert_eq!(code(call(&t.edit, empty).await), "invalid_input");
}

#[tokio::test]
async fn edit_after_a_windowed_read_covers_only_the_lines_seen() {
    let t = tools();
    t.ws.put("a.txt", "l1\nl2\nl3\nl4\n");
    call(
        &t.read,
        json!({"file_path": "a.txt", "offset": 1, "limit": 2}),
    )
    .await
    .unwrap();
    let unseen = json!({"file_path": "a.txt", "old_string": "l4", "new_string": "L4"});
    let error = call(&t.edit, unseen.clone()).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("read_before_write"));
    assert!(error.message.contains("lines 4-4"), "{}", error.message);
    let seen = json!({"file_path": "a.txt", "old_string": "l1\nl2", "new_string": "L1\nL2"});
    assert_eq!(text(call(&t.edit, seen).await), "Edited a.txt");
    // Lines may have moved, so windowed evidence is gone after the edit.
    let again = json!({"file_path": "a.txt", "old_string": "L1", "new_string": "l1"});
    assert_eq!(code(call(&t.edit, again).await), "read_before_write");
}

#[tokio::test]
async fn edit_refuses_a_file_changed_since_the_read() {
    let t = tools();
    t.ws.put("a.txt", "before\n");
    call(&t.read, json!({"file_path": "a.txt"})).await.unwrap();
    t.ws.put("a.txt", "before, then more\n");
    let edit = json!({"file_path": "a.txt", "old_string": "before", "new_string": "after"});
    assert_eq!(code(call(&t.edit, edit).await), "read_before_write");
    assert_eq!(t.ws.get("a.txt"), "before, then more\n");
}

#[tokio::test]
async fn tools_work_behind_shared_trait_objects() {
    let t = tools();
    let tool: Arc<dyn Tool> = Arc::new(Write::new(t.ws.fs.clone(), t.ws.reads.clone()));
    let out = call(tool.as_ref(), json!({"file_path": "x.txt", "content": "1"})).await;
    assert_eq!(text(out), "Wrote x.txt");
    let escape = call(
        tool.as_ref(),
        json!({"file_path": "/etc/x", "content": "1"}),
    )
    .await;
    assert_eq!(code(escape), "path_escape");
}
