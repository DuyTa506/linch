//! A compaction folds what the model read: a workspace agent must read a
//! file again before it edits it (Python's `reset_read_tracker`).

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

/// Read `a.txt`, read `b.txt`, then edit `a.txt`, under `compaction`; the
/// edit's result text and whether it failed.
async fn edit_after_two_reads(compaction: Option<Value>) -> (String, bool) {
    let dir = tempfile::tempdir().unwrap();
    // Long enough that a summary of what the read folded is smaller than it.
    let body = format!("alpha\n{}\n", "padding ".repeat(60));
    std::fs::write(dir.path().join("a.txt"), body).unwrap();
    std::fs::write(dir.path().join("b.txt"), "beta\n").unwrap();
    let read = |id: &str, path: &str| {
        TurnBuilder::new(MODEL)
            .tool_call(id, "Read", &json!({"file_path": path}))
            .build()
    };
    let edit = TurnBuilder::new(MODEL)
        .tool_call(
            "c3",
            "Edit",
            &json!({"file_path": "a.txt", "old_string": "alpha", "new_string": "gamma"}),
        )
        .build();
    let text = |text: &str| TurnBuilder::new(MODEL).text(text).build();
    let mut turns = vec![read("c1", "a.txt"), read("c2", "b.txt")];
    if compaction.is_some() {
        // The summary call before the third turn.
        turns.push(text("summary"));
    }
    turns.extend([edit, text("done")]);
    let mut spec = json!({"model": MODEL});
    if let Some(compaction) = compaction {
        spec["compaction"] = compaction;
    }
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": spec,
        "provider": {"id": "scripted", "turns": turns},
        "workspace": dir.path(),
        "permissions": {"mode": "skip_dangerous", "project_root": dir.path()},
    }))
    .unwrap();
    let opened = config.open().await.unwrap();
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event
            && end.tool_use_id() == "c3"
        {
            let result = serde_json::to_value(&end.result).unwrap();
            let text = result["content"][0]["text"].as_str().unwrap().to_owned();
            return (text, end.result.is_error);
        }
    }
    panic!("no edit result");
}

#[tokio::test]
async fn a_compaction_makes_the_model_read_again_before_an_edit() {
    let (_, failed) = edit_after_two_reads(None).await;
    assert!(!failed, "without compaction the read still counts");
    let compaction = json!({"threshold_tokens": 1, "keep_recent_turns": 1});
    let (text, failed) = edit_after_two_reads(Some(compaction)).await;
    assert!(failed, "{text}");
    assert!(text.contains("Read"), "{text}");
}
