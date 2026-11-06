//! A host's own child runs: the agent of a subagent definition, started
//! under a parent run id, and the cheap run-status read.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    AgentBuilder, BuildError, PermissionMode, RunError, RunId, RunOptions, RunStatus, RunStore,
    ScriptedProvider, StoreError, SubagentDefinition, ToolScope,
};

fn options(session: &str, run: &str) -> RunOptions {
    let mut options = RunOptions::new(session);
    options.run_id = Some(RunId::new(run));
    options
}

fn builder(provider: Arc<ScriptedProvider>, stores: &Arc<Stores>) -> AgentBuilder {
    let runs = Runs::default();
    let tools = registry(vec![
        Echo::new("Echo", ToolScope::Read, &runs),
        Echo::new("Write", ToolScope::Write, &runs),
    ]);
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
}

#[tokio::test]
async fn status_reads_a_run_status() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
    let stores = Arc::new(Stores::default());
    let agent = builder(provider, &stores).build().unwrap();
    finish(agent.run("go", options("s1", "r1"))).await;
    let status = agent.status(&RunId::new("r1")).await.unwrap();
    assert_eq!(status, RunStatus::Completed);
    let missing = agent.status(&RunId::new("nope")).await;
    assert!(matches!(
        missing,
        Err(RunError::Store(StoreError::NotFound { .. }))
    ));
}

#[tokio::test]
async fn a_subagent_runs_under_a_parent_run_with_its_definition() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("found it")]));
    let stores = Arc::new(Stores::default());
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    scout.tools = Some(vec!["Echo".into()]);
    let agent = builder(provider.clone(), &stores)
        .subagents(vec![scout])
        .build()
        .unwrap();

    let child = agent.subagent(Some("Scout")).unwrap();
    assert_ne!(child.contract(), agent.contract());
    let parent = RunId::new("wf-1");
    finish(child.run_child("look", options("c1", "c1"), parent.clone())).await;

    let record = stores.runs.load(NS, &RunId::new("c1")).await.unwrap();
    assert_eq!(record.parent_run_id, Some(parent));
    assert_eq!(record.status, RunStatus::Completed);
    let request = provider.requests().pop().unwrap();
    let names: Vec<&str> = request.tools.iter().map(|t| t.name.as_str()).collect();
    assert_eq!(names, ["Echo"]);
    let last = request.system.last().unwrap();
    assert_eq!(last.text, "User-provided instructions:\n\nScout the code.");
    let general = agent.subagent(None).unwrap();
    assert_ne!(general.contract(), child.contract());
}

#[tokio::test]
async fn a_subagent_needs_subagents_and_a_known_name() {
    let provider = Arc::new(ScriptedProvider::new([]));
    let stores = Arc::new(Stores::default());
    let bare = builder(provider.clone(), &stores).build().unwrap();
    assert!(matches!(
        bare.subagent(None),
        Err(BuildError::Invalid(message)) if message == "this agent has no subagents"
    ));
    let agent = builder(provider, &stores)
        .subagents(Vec::new())
        .build()
        .unwrap();
    assert!(matches!(
        agent.subagent(Some("nope")),
        Err(BuildError::Invalid(message)) if message == "unknown subagent type: nope"
    ));
    assert!(agent.subagent(Some("verification")).is_ok());
}
