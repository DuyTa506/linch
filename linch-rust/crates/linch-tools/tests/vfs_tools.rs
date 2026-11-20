//! The `ls`, `read_file`, `write_file` and `edit_file` tools over
//! per-session memory backends.

mod common;

use std::sync::Arc;

use common::{code, ctx, input, text};
use linch_core::vfs::MAX_FILE_BYTES;
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

/// R3-sec 5: a virtual file holds at most `MAX_FILE_BYTES`, so neither a
/// large write nor an edit that multiplies a file (`replace_all`) passes
/// it; the file stays as it was.
#[tokio::test]
async fn a_file_cannot_grow_past_the_size_cap() {
    let t = tools();
    call(&t.write, json!({"path": "/f", "content": "a"}))
        .await
        .unwrap();
    let times = |n: usize| {
        let new = "a".repeat(n);
        json!({"path": "/f", "old_string": "a", "new_string": new, "replace_all": true})
    };
    call(&t.edit, times(1000)).await.unwrap();
    call(&t.edit, times(1000)).await.unwrap();
    let grown = call(&t.edit, times(100)).await;
    let error = grown.expect_err("100 MB is past the cap");
    assert_eq!(error.code.as_deref(), Some("too_large"));
    assert!(error.message.contains("'/f'"), "{}", error.message);
    let same = text(call(&t.edit, times(1)).await);
    assert_eq!(
        same, "Made 1000000 replacements in '/f'.",
        "the file stays as it was"
    );

    let big = "b".repeat(MAX_FILE_BYTES + 1);
    let write = call(&t.write, json!({"path": "/g", "content": big})).await;
    assert_eq!(code(write), "too_large");
    let missing = call(&t.read, json!({"path": "/g"})).await;
    assert_eq!(code(missing), "not_found", "nothing was written");
    let full = "b".repeat(MAX_FILE_BYTES);
    call(&t.write, json!({"path": "/g", "content": full}))
        .await
        .unwrap();
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

/// The most bytes of one line `read_file` returns in one call.
const LINE_PAGE: usize = 16 * 1024;

async fn write(t: &Tools, path: &str, content: &str) {
    let written = call(&t.write, json!({"path": path, "content": content})).await;
    written.unwrap();
}

/// Reads line `line` of `path` page by page, following the continuation
/// hint, and returns the pages and the text they add up to.
async fn page_line(t: &Tools, path: &str, line: usize) -> (Vec<String>, String) {
    let mut pages = Vec::new();
    let mut whole = String::new();
    let mut next = json!({"path": path, "offset": line, "limit": 1});
    loop {
        let out = text(call(&t.read, next.clone()).await);
        let marker = format!("\n[line {line} continues at byte ");
        let Some((page, rest)) = out.rsplit_once(&marker) else {
            whole.push_str(&out);
            pages.push(out);
            return (pages, whole);
        };
        let at: usize = rest.split(';').next().unwrap().parse().unwrap();
        let hint = format!(
            "[line {line} continues at byte {at}; read_file(path=\"{path}\", offset={line}, \
             limit=1, byte_offset={at})]"
        );
        assert!(
            out.ends_with(&hint),
            "the output ends with the exact hint: {}",
            &out[page.len()..]
        );
        whole.push_str(page);
        pages.push(out.clone());
        next = json!({"path": path, "offset": line, "limit": 1, "byte_offset": at});
    }
}

#[tokio::test]
async fn one_huge_line_is_paged_by_byte_offset_to_its_end() {
    let t = tools();
    let line: String = (0..500_000usize)
        .map(|i| char::from(b'a' + (i % 26) as u8))
        .collect();
    write(&t, "/big", &line).await;
    let (pages, whole) = page_line(&t, "/big", 1).await;
    assert_eq!(whole, line, "the pages add up to the exact bytes");
    assert_eq!(pages.len(), 500_000_usize.div_ceil(LINE_PAGE));
    assert!(pages.iter().all(|page| page.len() < LINE_PAGE + 200));
    assert!(pages[0].starts_with(&line[..LINE_PAGE]));
    assert!(pages[0].contains("continues at byte 16384;"));
}

#[tokio::test]
async fn a_page_is_cut_on_a_character_boundary() {
    let t = tools();
    // Three bytes per character; 16384 is not a multiple of 3.
    let line = "€".repeat(20_000);
    write(&t, "/wide", &line).await;
    let (pages, whole) = page_line(&t, "/wide", 1).await;
    assert_eq!(whole, line);
    assert!(
        pages[0].contains("continues at byte 16383;"),
        "cut at the boundary below 16384"
    );
}

#[tokio::test]
async fn a_long_line_among_short_ones_is_cut_in_place() {
    let t = tools();
    let long = "x".repeat(LINE_PAGE + 10);
    write(&t, "/mix", &format!("first\n{long}\nlast\n")).await;
    let hint = "[line 2 continues at byte 16384; read_file(path=\"/mix\", offset=2, \
                limit=1, byte_offset=16384)]";
    let want = format!("first\n{}\n{hint}\nlast", &long[..LINE_PAGE]);
    for args in [
        json!({"path": "/mix"}),
        json!({"path": "/mix", "offset": 1, "limit": 3}),
    ] {
        assert_eq!(text(call(&t.read, args).await), want);
    }
    let (_, whole) = page_line(&t, "/mix", 2).await;
    assert_eq!(whole, long);
}

#[tokio::test]
async fn files_with_no_long_line_read_byte_for_byte_as_before() {
    let t = tools();
    let at_bound = "y".repeat(LINE_PAGE);
    let body = format!("a\n{at_bound}\nc\n");
    write(&t, "/ok", &body).await;
    assert_eq!(text(call(&t.read, json!({"path": "/ok"})).await), body);
    let window = call(&t.read, json!({"path": "/ok", "offset": 2, "limit": 2})).await;
    assert_eq!(
        text(window),
        format!("{at_bound}\nc"),
        "a line at the bound is whole"
    );
    let many: String = (1..=5000).map(|n| format!("line {n}\n")).collect();
    write(&t, "/many", &many).await;
    assert_eq!(text(call(&t.read, json!({"path": "/many"})).await), many);
}

#[tokio::test]
async fn byte_offset_needs_one_line_and_a_valid_position() {
    let t = tools();
    write(&t, "/w", "€€€\nsecond").await;
    let with = |extra: Value| {
        let mut args = json!({"path": "/w", "offset": 1, "limit": 1});
        args.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        args
    };
    let read = |extra: Value| call(&t.read, with(extra));
    assert_eq!(text(read(json!({"byte_offset": 3})).await), "€€");
    assert_eq!(text(read(json!({"byte_offset": 0})).await), "€€€");
    for past in [9, 10] {
        let out = text(read(json!({"byte_offset": past})).await);
        assert_eq!(
            out,
            "<no bytes at byte_offset {past}; line 1 has 9 bytes>"
                .replace("{past}", &past.to_string())
        );
    }
    for bad in [
        json!({"byte_offset": 1}),
        json!({"limit": 2, "byte_offset": 3}),
    ] {
        assert_eq!(code(read(bad).await), "invalid_input");
    }
    let no_limit = call(&t.read, json!({"path": "/w", "byte_offset": 3})).await;
    assert_eq!(code(no_limit), "invalid_input");
}

/// The names, descriptions and input schemas of the four tools are part of
/// the run contract of an agent with `filesystem`: a stored run resumes only
/// while they are byte for byte what it was opened with. Change one on
/// purpose and this digest moves; then say so in the changelog.
#[test]
fn the_tool_schemas_of_an_agent_with_filesystem_keep_their_digest() {
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    let listed: Vec<Value> = vfs_tools(files)
        .iter()
        .map(|tool| {
            json!({
                "name": tool.name(),
                "description": tool.description(),
                "input_schema": tool.input_schema(),
            })
        })
        .collect();
    let digest = linch_core::digest(&Value::Array(listed)).unwrap();
    assert_eq!(
        digest.to_string(),
        "c73dbe3692030c186d19d480e2cfb9ed7a75f76b8a67b82232598bf9fe4c6954",
        "the tool schemas changed: a stored run of an agent with filesystem would not resume"
    );
}
