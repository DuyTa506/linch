//! A subagent's child run keeps the contract it started with: the parent's
//! tools and hooks as they were when the parent's agent was built, plus the
//! child's definition. A tool registered later does not change it; a child
//! whose contract no longer matches ends `failed` instead of staying
//! `running` with its session busy.

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use async_trait::async_trait;
use common::subagents::*;
use common::*;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, PermissionMode, Provider, ProviderError, ProviderRequest, ProviderStream,
    RunId, RunOptions, RunStatus, RunStore, RunStream, ScriptedTurn, SubagentDefinition, ToolScope,
    Workers,
};
use serde_json::json;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

/// Answers like [`Family`]; the first child call ever waits for good,
/// after telling `entered`.
struct HoldFirst {
    family: Family,
    entered: Notify,
    held: AtomicBool,
}

#[async_trait]
impl Provider for HoldFirst {
    fn id(&self) -> &str {
        "hold-first"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if who(&request) != "parent" && !self.held.swap(true, Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        self.family.stream(request, cancel).await
    }
}

fn spawn(kind: Option<&str>) -> ScriptedTurn {
    let mut input = json!({"description": "look", "prompt": "check x", "run_in_background": true});
    if let Some(kind) = kind {
        input["subagent_type"] = json!(kind);
    }
    TurnBuilder::new(MODEL)
        .tool_call("sub_0", "Subagent", &input)
        .usage(usage(10, 5))
        .build()
}

fn provider(kind: Option<&str>) -> Arc<HoldFirst> {
    let parent = vec![spawn(kind), text_turn("waiting"), text_turn("done")];
    Arc::new(HoldFirst {
        family: Family::new(parent, vec![text_turn("x holds")]),
        entered: Notify::new(),
        held: AtomicBool::new(false),
    })
}

fn agent(
    provider: &Arc<HoldFirst>,
    stores: &Arc<Stores>,
    tools: &Arc<linch_core::ToolRegistry>,
    definitions: Vec<SubagentDefinition>,
) -> Agent {
    AgentBuilder::new(spec())
        .provider(provider.clone())
        .tools(tools.clone())
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(definitions)
        .workers(Workers {
            background: true,
            ..Workers::default()
        })
        .build()
        .unwrap()
}

/// Drive `stream` until the child is inside its model call, then drop it
/// there, like a crash.
async fn drop_mid_child(stream: RunStream, entered: &Notify) {
    let mut stream = stream;
    let mut entered = std::pin::pin!(entered.notified());
    loop {
        tokio::select! {
            () = &mut entered => return,
            item = stream.next() => assert!(item.is_some(), "the run ended first"),
        }
    }
}

fn r1() -> RunOptions {
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    options
}

#[tokio::test]
async fn a_tool_registered_after_the_child_started_does_not_change_its_contract() {
    let (provider, stores) = (provider(None), Arc::new(Stores::default()));
    let tools = registry(Vec::new());
    let agent = agent(&provider, &stores, &tools, Vec::new());
    drop_mid_child(agent.run("go", r1()), &provider.entered).await;
    let _late = tools
        .register(Arc::new(Echo::new(
            "Late",
            ToolScope::Read,
            &Runs::default(),
        )))
        .unwrap();

    finish(agent.resume(RunId::new("r1"), RunOptions::new("s1"))).await;
    let child = stores.runs.load(NS, &RunId::new("r1:sub_0")).await.unwrap();
    assert_eq!(child.status, RunStatus::Completed);
    let log = messages(&*stores, "s1").await;
    let all: String = log.iter().map(|m| m.text()).collect();
    assert!(all.contains("<status>completed</status>"), "{all}");
    assert!(all.contains("<result>x holds</result>"), "{all}");
}

/// A restarted host reopens the agent as it was, then reloads a changed
/// definition before it resumes the parent.
#[tokio::test]
async fn a_child_of_another_contract_ends_failed_and_frees_its_session() {
    let (provider, stores) = (provider(Some("scout")), Arc::new(Stores::default()));
    let scout = |body: &str| SubagentDefinition::new("scout", "Scouts.", body);
    let first = agent(
        &provider,
        &stores,
        &registry(Vec::new()),
        vec![scout("Look around.")],
    );
    drop_mid_child(first.run("go", r1()), &provider.entered).await;
    drop(first);
    let agent = agent(
        &provider,
        &stores,
        &registry(Vec::new()),
        vec![scout("Look around.")],
    );
    let _reload = agent
        .reload_subagents(vec![scout("Look elsewhere.")])
        .unwrap();

    finish(agent.resume(RunId::new("r1"), RunOptions::new("s1"))).await;
    let child = stores.runs.load(NS, &RunId::new("r1:sub_0")).await.unwrap();
    assert_eq!(child.status, RunStatus::Failed, "the child ended");
    let log = messages(&*stores, "s1").await;
    let all: String = log.iter().map(|m| m.text()).collect();
    assert!(all.contains("<status>failed</status>"), "{all}");
    assert!(all.contains("contract"), "{all}");
    let parent = stores.runs.load(NS, &RunId::new("r1")).await.unwrap();
    assert_eq!(parent.status, RunStatus::Completed);
}
