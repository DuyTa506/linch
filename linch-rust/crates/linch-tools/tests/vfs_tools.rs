//! The `ls`, `read_file`, `write_file` and `edit_file` tools over
//! per-session memory backends.

mod common;

use std::sync::Arc;

use common::{code, ctx, input, text};
use linch_core::{Tool, ToolResult, ToolScope};
use linch_tools::vfs::{
    EditFile, Ls, MemorySessions, PROMPT, ReadFile, SessionFiles, WriteFile, vfs_tools,
};
use serde_json::{Value, json};

struct Tools {
    ls: Ls,
    read: ReadFile,
    write: WriteFile,
    edit: EditFile,
}

fn tools() -> Tools {
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    Tools {
        ls: Ls::new(files.clone()),
        read: ReadFile::new(files.clone()),
        write: WriteFile::new(files.clone()),
        edit: EditFile::new(files),
    }
}

async fn run(tool: &dyn Tool, session: &str, value: Value) -> ToolResult {
    tool.execute(input(value), ctx(session)).await
}

async fn call(tool: &dyn Tool, value: Value) -> ToolResult {
    run(tool, "s1", value).await
}

#[test]
fn names_scopes_and_path_fields_match_the_permission_table() {
    let t = tools();
    let table: [(&dyn Tool, &str, ToolScope, &str); 4] = [
        (&t.ls, "ls", ToolScope::Read, "prefix"),
        (&t.read, "read_file", ToolScope::Read, "path"),
        (&t.write, "write_file", ToolScope::Write, "path"),
        (&t.edit, "edit_file", ToolScope::Write, "path"),
    ];
    for (tool, name, scope, field) in table {
        assert_eq!(tool.name(), name);
        assert_eq!(tool.scope(), scope, "{name}");
        let properties = tool.input_schema()["properties"].as_object().unwrap();
        assert!(properties.contains_key(field), "{name} has {field}");
    }
    let all = vfs_tools(Arc::new(MemorySessions::new()));
    let names: Vec<&str> = all.iter().map(|tool| tool.name()).collect();
    assert_eq!(names, ["ls", "read_file", "write_file", "edit_file"]);
}

#[test]
fn summaries_name_the_call() {
    let t = tools();
    assert_eq!(t.ls.summarize(&input(json!({}))), "ls()");
    assert_eq!(
        t.ls.summarize(&input(json!({"prefix": "/o"}))),
        "ls(\"/o\")"
    );
    let window = input(json!({"path": "/a", "offset": 2, "limit": 3}));
    assert_eq!(
        t.read.summarize(&window),
        "read_file(\"/a\", offset=2, limit=3)"
    );
    let write = input(json!({"path": "/a", "content": "x"}));
    assert_eq!(t.write.summarize(&write), "write_file(\"/a\")");
}

#[tokio::test]
async fn write_then_read_back_whole_and_windowed() {
    let t = tools();
    let written = call(
        &t.write,
        json!({"path": "notes//plan.md", "content": "a\nb\nc\n"}),
    )
    .await;
    assert_eq!(text(written), "Written 3 lines to '/notes/plan.md'.");
    let whole = call(&t.read, json!({"path": " /notes/plan.md "})).await;
    assert_eq!(text(whole), "a\nb\nc\n", "an unwindowed read is exact");
    let window = call(
        &t.read,
        json!({"path": "/notes/plan.md", "offset": 2, "limit": 1}),
    )
    .await;
    assert_eq!(text(window), "b");
    let tail = call(&t.read, json!({"path": "/notes/plan.md", "offset": 2})).await;
    assert_eq!(text(tail), "b\nc");
    let past = call(&t.read, json!({"path": "/notes/plan.md", "offset": 9})).await;
    assert_eq!(
        text(past),
        "<no lines at offset 9; /notes/plan.md has 3 lines>"
    );

    let empty = call(&t.write, json!({"path": "/e", "content": ""})).await;
    assert_eq!(text(empty), "Written 0 lines to '/e'.");
    assert_eq!(
        text(call(&t.read, json!({"path": "/e"})).await),
        "<file is empty>"
    );
    let one = call(&t.write, json!({"path": "/one", "content": "x"})).await;
    assert_eq!(text(one), "Written 1 line to '/one'.");
}

#[tokio::test]
async fn a_missing_file_points_at_ls() {
    let t = tools();
    let error = call(&t.read, json!({"path": "/nope"})).await.unwrap_err();
    assert_eq!(
        error.message,
        "File not found: '/nope'. Use ls() to see available files."
    );
    assert_eq!(error.code.as_deref(), Some("not_found"));
    let edit = json!({"path": "/nope", "old_string": "a", "new_string": "b"});
    assert_eq!(code(call(&t.edit, edit).await), "not_found");
}

#[tokio::test]
async fn bad_paths_are_invalid_path_errors() {
    let t = tools();
    for path in ["/a/../b", "/", "   "] {
        let read = call(&t.read, json!({"path": path})).await;
        let expected = if path.trim().is_empty() {
            "invalid_input"
        } else {
            "invalid_path"
        };
        assert_eq!(code(read), expected, "{path:?}");
    }
    let ls = call(&t.ls, json!({"prefix": "/.."})).await;
    assert_eq!(code(ls), "invalid_path");
}

#[tokio::test]
async fn ls_lists_sorted_paths_under_a_prefix() {
    let t = tools();
    assert_eq!(text(call(&t.ls, json!({})).await), "No files found.");
    for path in ["/offload/b.txt", "/offload/a.txt", "/notes.md"] {
        call(&t.write, json!({"path": path, "content": "x"}))
            .await
            .unwrap();
    }
    assert_eq!(
        text(call(&t.ls, json!({})).await),
        "/notes.md\n/offload/a.txt\n/offload/b.txt"
    );
    let offload = call(&t.ls, json!({"prefix": "/offload"})).await;
    assert_eq!(text(offload), "/offload/a.txt\n/offload/b.txt");
    let none = call(&t.ls, json!({"prefix": "/memories"})).await;
    assert_eq!(text(none), "No files found under '/memories'.");
}

#[tokio::test]
async fn edit_replaces_and_reports_each_refusal() {
    let t = tools();
    call(&t.write, json!({"path": "/f", "content": "x y x"}))
        .await
        .unwrap();
    let ambiguous = json!({"path": "/f", "old_string": "x", "new_string": "z"});
    assert_eq!(code(call(&t.edit, ambiguous).await), "ambiguous_match");
    let absent = json!({"path": "/f", "old_string": "q", "new_string": "z"});
    assert_eq!(code(call(&t.edit, absent).await), "no_match");
    let empty = json!({"path": "/f", "old_string": "", "new_string": "z"});
    assert_eq!(code(call(&t.edit, empty).await), "invalid_input");

    let one = json!({"path": "/f", "old_string": "y", "new_string": "Y"});
    assert_eq!(
        text(call(&t.edit, one).await),
        "Made 1 replacement in '/f'."
    );
    let all = json!({"path": "/f", "old_string": "x", "new_string": "z", "replace_all": true});
    assert_eq!(
        text(call(&t.edit, all).await),
        "Made 2 replacements in '/f'."
    );
    assert_eq!(text(call(&t.read, json!({"path": "/f"})).await), "z Y z");
}

#[tokio::test]
async fn sessions_do_not_see_each_others_files() {
    let t = tools();
    run(&t.write, "s1", json!({"path": "/mine", "content": "s1"}))
        .await
        .unwrap();
    let other = run(&t.read, "s2", json!({"path": "/mine"})).await;
    assert_eq!(code(other), "not_found");
    let listed = run(&t.ls, "s2", json!({})).await;
    assert_eq!(text(listed), "No files found.");
}

#[test]
fn the_prompt_names_the_tools_and_the_offload_pointer() {
    for tool in ["ls", "read_file", "write_file", "edit_file"] {
        assert!(PROMPT.contains(tool), "{tool}");
    }
    assert!(PROMPT.contains("read_file(path, offset, limit)"));
}
