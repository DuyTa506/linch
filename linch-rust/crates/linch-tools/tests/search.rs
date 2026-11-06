//! `Grep` and `Glob` on a temp workspace.

mod common;

use std::fs::File;
use std::time::{Duration, SystemTime};

use common::{Workspace, call, code, ctx, input, text};
use linch_core::{Tool, ToolScope};
use linch_tools::{Glob, Grep};
use serde_json::json;

fn workspace() -> Workspace {
    let ws = Workspace::new();
    ws.put("src/main.rs", "fn main() {\n    println!(\"Hello\");\n}\n");
    ws.put("src/lib.rs", "// hello\npub fn hello() {}\n");
    ws.put("docs/guide.md", "Say hello to the guide.\n");
    ws.put("target/out.rs", "fn hello_built() {}\n");
    ws.put(".gitignore", "target/\n");
    ws.put(".hidden/notes.rs", "fn hello_hidden() {}\n");
    ws.put("blob.bin", "hello\0binary\n");
    ws
}

fn grep(ws: &Workspace) -> Grep {
    Grep::new(ws.fs.clone())
}

fn glob(ws: &Workspace) -> Glob {
    Glob::new(ws.fs.clone())
}

#[tokio::test]
async fn grep_lists_matching_files_and_skips_ignored_hidden_and_binary_ones() {
    let ws = workspace();
    let tool = grep(&ws);
    assert_eq!(tool.scope(), ToolScope::Read);
    let out = text(call(&tool, json!({"pattern": "hello"})).await);
    assert_eq!(out, "docs/guide.md\nsrc/lib.rs");
    let out = text(call(&tool, json!({"pattern": "hello", "-i": true})).await);
    assert_eq!(out, "docs/guide.md\nsrc/lib.rs\nsrc/main.rs");
    let none = text(call(&tool, json!({"pattern": "absent_word"})).await);
    assert_eq!(none, "No matches found.");
}

#[tokio::test]
async fn grep_content_mode_prints_lines_numbers_and_context() {
    let ws = Workspace::new();
    ws.put("a.txt", "l1\nhit one\nl3\nl4\nl5\nl6\nhit two\nl8\n");
    let tool = grep(&ws);
    let plain = text(call(&tool, json!({"pattern": "hit", "output_mode": "content"})).await);
    assert_eq!(plain, "a.txt:hit one\na.txt:hit two");
    let with_context = json!({"pattern": "hit", "output_mode": "content", "-n": true, "-C": 1});
    let out = text(call(&tool, with_context).await);
    let expected =
        "a.txt-1-l1\na.txt:2:hit one\na.txt-3-l3\n--\na.txt-6-l6\na.txt:7:hit two\na.txt-8-l8";
    assert_eq!(out, expected);
    let after = json!({"pattern": "one", "output_mode": "content", "-n": true, "-A": 1});
    assert_eq!(
        text(call(&tool, after).await),
        "a.txt:2:hit one\na.txt-3-l3"
    );
}

#[tokio::test]
async fn grep_counts_and_matches_across_lines() {
    let ws = Workspace::new();
    ws.put("a.txt", "x\nx\ny\n");
    ws.put("b.txt", "start\nmiddle\nend\n");
    let tool = grep(&ws);
    let count = text(call(&tool, json!({"pattern": "x", "output_mode": "count"})).await);
    assert_eq!(count, "a.txt:2");
    let single = call(&tool, json!({"pattern": "start.*end"})).await;
    assert_eq!(text(single), "No matches found.");
    let multi =
        json!({"pattern": "start.*end", "multiline": true, "output_mode": "content", "-n": true});
    assert_eq!(
        text(call(&tool, multi).await),
        "b.txt:1:start\nb.txt:2:middle\nb.txt:3:end"
    );
}

#[tokio::test]
async fn grep_filters_by_glob_type_and_path() {
    let ws = workspace();
    let tool = grep(&ws);
    let by_glob = text(
        call(
            &tool,
            json!({"pattern": "hello", "-i": true, "glob": "*.md"}),
        )
        .await,
    );
    assert_eq!(by_glob, "docs/guide.md");
    let by_type = text(
        call(
            &tool,
            json!({"pattern": "hello", "-i": true, "type": "rust"}),
        )
        .await,
    );
    assert_eq!(by_type, "src/lib.rs\nsrc/main.rs");
    let in_dir = text(
        call(
            &tool,
            json!({"pattern": "hello", "-i": true, "path": "src"}),
        )
        .await,
    );
    assert_eq!(in_dir, "src/lib.rs\nsrc/main.rs");
    // An explicit path is searched even when it is ignored.
    let file = text(call(&tool, json!({"pattern": "hello", "path": "target/out.rs"})).await);
    assert_eq!(file, "target/out.rs");
}

#[tokio::test]
async fn grep_rejects_bad_queries_and_escaping_paths() {
    let ws = workspace();
    let tool = grep(&ws);
    assert_eq!(
        code(call(&tool, json!({"pattern": "("})).await),
        "invalid_query"
    );
    let bad_type = call(&tool, json!({"pattern": "x", "type": "no-such-type"})).await;
    assert_eq!(code(bad_type), "invalid_query");
    let bad_mode = call(&tool, json!({"pattern": "x", "output_mode": "lines"})).await;
    assert_eq!(code(bad_mode), "invalid_input");
    let escape = call(&tool, json!({"pattern": "x", "path": ".."})).await;
    assert_eq!(code(escape), "path_escape");
}

#[tokio::test]
async fn grep_stops_at_head_limit_and_says_so() {
    let ws = Workspace::new();
    for name in ["a", "b", "c"] {
        ws.put(&format!("{name}.txt"), "m\nm\nm\n");
    }
    let tool = grep(&ws);
    let files = text(call(&tool, json!({"pattern": "m", "head_limit": 2})).await);
    assert_eq!(files, "a.txt\nb.txt\n… (truncated at head_limit 2)");
    let lines = json!({"pattern": "m", "output_mode": "content", "head_limit": 4});
    assert_eq!(
        text(call(&tool, lines).await),
        "a.txt:m\na.txt:m\na.txt:m\nb.txt:m\n… (truncated at head_limit 4)"
    );
    let exact = json!({"pattern": "m", "output_mode": "content", "head_limit": 9});
    assert!(!text(call(&tool, exact).await).contains("truncated"));
}

#[tokio::test]
async fn grep_caps_long_lines_and_large_output() {
    let ws = Workspace::new();
    ws.put("long.txt", &format!("{}\n", "a".repeat(2_000)));
    let tool = grep(&ws);
    let out = text(call(&tool, json!({"pattern": "a", "output_mode": "content"})).await);
    assert!(out.ends_with(" [... line truncated]"), "{out}");
    assert!(out.len() < 600);
    let many: String = (0..1_000)
        .map(|i| format!("match {i} {}\n", "z".repeat(100)))
        .collect();
    ws.put("many.txt", &many);
    let big = json!({"pattern": "match", "output_mode": "content", "head_limit": 1000});
    let out = text(call(&tool, big).await);
    assert!(
        out.ends_with("(output capped at 30000 bytes)"),
        "{}",
        &out[out.len() - 80..]
    );
}

#[tokio::test]
async fn searches_return_cancelled_once_the_call_is_cancelled() {
    let ws = workspace();
    let cancelled = ctx("s1");
    cancelled.cancel.cancel();
    let result = grep(&ws)
        .execute(input(json!({"pattern": "x"})), cancelled.clone())
        .await;
    assert_eq!(code(result), "cancelled");
    let result = glob(&ws)
        .execute(input(json!({"pattern": "*"})), cancelled)
        .await;
    assert_eq!(code(result), "cancelled");
}

#[tokio::test]
async fn glob_matches_names_at_any_depth_or_anchored_paths() {
    let ws = workspace();
    let tool = glob(&ws);
    assert_eq!(tool.scope(), ToolScope::Read);
    let set = |path: &str, secs: u64| {
        let file = File::options()
            .write(true)
            .open(ws.root().join(path))
            .unwrap();
        file.set_modified(SystemTime::UNIX_EPOCH + Duration::from_secs(secs))
            .unwrap();
    };
    set("src/main.rs", 2_000);
    set("src/lib.rs", 1_000);
    let any_depth = text(call(&tool, json!({"pattern": "*.rs"})).await);
    assert_eq!(any_depth, "src/main.rs\nsrc/lib.rs");
    let anchored = text(call(&tool, json!({"pattern": "src/l*.rs"})).await);
    assert_eq!(anchored, "src/lib.rs");
    let in_dir = text(call(&tool, json!({"pattern": "*.md", "path": "docs"})).await);
    assert_eq!(in_dir, "docs/guide.md");
    let none = text(call(&tool, json!({"pattern": "*.zig"})).await);
    assert_eq!(none, "No matches found.");
}

#[tokio::test]
async fn glob_shows_hidden_files_only_when_the_pattern_names_them() {
    let ws = workspace();
    ws.put(".env.local", "K=V\n");
    let tool = glob(&ws);
    let hidden = text(call(&tool, json!({"pattern": ".hidden/*.rs"})).await);
    assert_eq!(hidden, ".hidden/notes.rs");
    assert_eq!(
        text(call(&tool, json!({"pattern": ".env*"})).await),
        ".env.local"
    );
    let plain = text(call(&tool, json!({"pattern": "**/notes.rs"})).await);
    assert_eq!(plain, "No matches found.");
}

#[tokio::test]
async fn glob_rejects_escaping_patterns_and_paths() {
    let ws = workspace();
    let tool = glob(&ws);
    assert_eq!(
        code(call(&tool, json!({"pattern": "../*"})).await),
        "invalid_query"
    );
    assert_eq!(
        code(call(&tool, json!({"pattern": "/etc/*"})).await),
        "invalid_query"
    );
    let escape = call(&tool, json!({"pattern": "*", "path": "../outside"})).await;
    assert_eq!(code(escape), "path_escape");
    let file = call(&tool, json!({"pattern": "*", "path": "src/lib.rs"})).await;
    assert_eq!(code(file), "not_a_directory");
}

#[tokio::test]
async fn glob_caps_results_at_one_thousand() {
    let ws = Workspace::new();
    for i in 0..1_005 {
        ws.put(&format!("f{i:04}.txt"), "");
    }
    let out = text(call(&glob(&ws), json!({"pattern": "*.txt"})).await);
    assert_eq!(out.lines().count(), 1_001);
    assert!(out.ends_with("… (truncated to 1000 of 1005 results)"));
}
