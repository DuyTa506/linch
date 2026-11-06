//! Opening an agent with skills and subagents on: they load from the
//! config directory after every other tool is registered, and stay off
//! unless the config turns them on.

use std::path::Path;
use std::sync::Arc;

use futures::StreamExt;
use linch::project::SkipReason;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, Hooks, OpenedAgent, PgPools, RunOptions, ToolRegistry};
use serde_json::{Value, json};

fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

/// A config whose model calls `Skill` with `skill` and `args`, then ends.
fn config(skill: &str, extra: Value) -> AgentConfig {
    let turns = json!([
        TurnBuilder::new("scripted-model")
            .tool_call("call_1", "Skill", &json!({"skill": skill, "args": "prod"}))
            .build(),
        TurnBuilder::new("scripted-model").text("Done.").build(),
    ]);
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": turns},
    });
    value
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

async fn open(config: AgentConfig, tools: &Arc<ToolRegistry>) -> OpenedAgent {
    let hooks = Arc::new(Hooks::default());
    config
        .open_with(tools.clone(), hooks, &PgPools::new())
        .await
        .unwrap()
}

/// The text the `Skill` call returned, and the tools the run had.
async fn skill_result(opened: &OpenedAgent) -> (String, Vec<String>) {
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let (mut result, mut tools) = (None, Vec::new());
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::Init(init) => tools = init.tools,
            Event::ToolCallEnd(end) => result = Some(serde_json::to_value(&end.result).unwrap()),
            _ => {}
        }
    }
    let result = result.expect("the Skill call ended");
    (
        result["content"][0]["text"].as_str().unwrap().to_owned(),
        tools,
    )
}

#[tokio::test]
async fn skills_and_subagents_load_from_the_workspace_config_dir() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let skills = root.join(".linch/skills");
    write(
        &skills.join("deploy/SKILL.md"),
        "---\ndescription: Ship it.\n---\nDeploy from ${SKILL_DIR} to $ARGUMENTS.",
    );
    write(
        &skills.join("read/SKILL.md"),
        "---\nname: Read\ndescription: Clash.\n---\n",
    );
    write(
        &root.join(".linch/agents/scout.md"),
        "---\ndescription: Reads around.\ntools: [Read]\n---\nScout.",
    );
    let tools = Arc::new(ToolRegistry::new());
    let extra = json!({"workspace": root, "skills": true, "subagents": true});
    let opened = open(config("deploy", extra), &tools).await;

    let skipped: Vec<(String, SkipReason)> = (opened.skipped().iter())
        .map(|s| (s.path.display().to_string(), s.reason))
        .collect();
    let clash = skills.join("read").display().to_string();
    assert_eq!(skipped, [(clash, SkipReason::NameCollisionTool)]);
    let snapshot = tools.snapshot();
    let subagent = snapshot.resolve("Subagent").unwrap().tool();
    let listed = subagent.description();
    assert!(listed.contains("- scout: Reads around."), "{listed}");
    assert!(listed.contains("- verification: "), "{listed}");

    let (text, names) = skill_result(&opened).await;
    let deploy = skills.join("deploy").display().to_string();
    assert_eq!(
        text,
        format!("Skill base directory: {deploy}\n\nDeploy from {deploy} to prod.")
    );
    assert!(names.iter().any(|n| n == "Skill") && names.iter().any(|n| n == "Subagent"));
    opened.close().await;
}

#[tokio::test]
async fn a_config_dir_of_its_own_needs_no_workspace() {
    let dir = tempfile::tempdir().unwrap();
    write(
        &dir.path().join("skills/notes/SKILL.md"),
        "---\ndescription: Take notes.\n---\nNotes for $ARGUMENTS.",
    );
    let tools = Arc::new(ToolRegistry::new());
    let extra = json!({"skills": true, "config_dir": dir.path()});
    let opened = open(config("notes", extra), &tools).await;
    let (text, names) = skill_result(&opened).await;
    assert!(text.ends_with("\n\nNotes for prod."), "{text}");
    assert_eq!(names, ["Skill"], "skills alone: no Subagent tool");
}

#[tokio::test]
async fn skills_and_subagents_stay_off_unless_turned_on() {
    let dir = tempfile::tempdir().unwrap();
    write(
        &dir.path().join(".linch/skills/notes/SKILL.md"),
        "---\ndescription: Take notes.\n---\nNotes.",
    );
    let tools = Arc::new(ToolRegistry::new());
    let opened = open(config("notes", json!({"workspace": dir.path()})), &tools).await;
    let snapshot = tools.snapshot();
    assert!(snapshot.resolve("Skill").is_none() && snapshot.resolve("Subagent").is_none());
    assert!(opened.skipped().is_empty());
    let (text, _) = skill_result(&opened).await;
    assert!(text.contains("Skill"), "an unknown tool: {text}");
}

#[tokio::test]
async fn workers_and_a_mailbox_add_their_tools() {
    let dir = tempfile::tempdir().unwrap();
    let tools = Arc::new(ToolRegistry::new());
    let extra = json!({
        "config_dir": dir.path(),
        "subagents": true,
        "workers": {"background": true},
        "mailbox": true,
    });
    let opened = open(config("notes", extra), &tools).await;
    let snapshot = tools.snapshot();
    for name in ["Subagent", "SubagentContinue", "TaskStop", "send_message"] {
        assert!(snapshot.resolve(name).is_some(), "{name}");
    }
    opened.close().await;

    let no_subagents = config("notes", json!({"workers": {"retain": true}}));
    let hooks = Arc::new(Hooks::default());
    let opened = no_subagents
        .open_with(Arc::new(ToolRegistry::new()), hooks, &PgPools::new())
        .await;
    let Err(error) = opened else {
        panic!("workers need subagents");
    };
    assert!(
        error.to_string().contains("workers need subagents"),
        "{error}"
    );
}
