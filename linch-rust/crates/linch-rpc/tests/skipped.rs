//! `agent.open` reports the skill directories and subagent files of the
//! config directory it left out, and why.

mod common;

use common::*;
use linch_rpc::protocol::method;
use serde_json::{Value, json};

#[tokio::test]
async fn an_open_names_each_definition_it_skipped() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    std::fs::create_dir_all(root.join(".linch/skills/empty")).unwrap();
    std::fs::create_dir_all(root.join(".linch/agents")).unwrap();
    std::fs::write(root.join(".linch/agents/plain.md"), "No frontmatter.").unwrap();
    let rpc = Rpc::new();
    let mut config = scripted(vec![], None, "");
    config["workspace"] = json!(root);
    config["skills"] = json!(true);
    config["subagents"] = json!(true);

    let opened = rpc.call(method::AGENT_OPEN, config).await.unwrap();
    let mut skipped: Vec<(String, String)> = opened["skipped"]
        .as_array()
        .expect("a skipped list")
        .iter()
        .map(|entry| {
            assert!(entry["detail"].as_str().is_some_and(|d| !d.is_empty()));
            let path = entry["path"].as_str().unwrap().to_owned();
            (path, entry["reason"].as_str().unwrap().to_owned())
        })
        .collect();
    skipped.sort();
    let path = |rest: &str| root.join(rest).display().to_string();
    assert_eq!(
        skipped,
        [
            (path(".linch/agents/plain.md"), "missing_frontmatter".into()),
            (path(".linch/skills/empty"), "missing_skill_md".into()),
        ]
    );

    let clean = rpc
        .call(method::AGENT_OPEN, scripted(vec![], None, ""))
        .await;
    let clean: Value = clean.unwrap();
    assert!(clean.get("skipped").is_none(), "{clean}");
}
