//! The `tool_execute` waterfall and `post_tool_use` result mutation in a
//! run: layer order, a veto, no change without a listener, and a mutated
//! result reaching the model, the session and the log.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::events::kinds::ToolCallEndEvent;
use linch_core::hooks::{Next, PostToolUse, PostToolUseResponse, ToolCall, ToolMiddleware};
use linch_core::{
    Agent, AgentBuilder, Event, Hooks, PermissionMode, RunOptions, ScriptedProvider,
    ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

type Log = Arc<Mutex<Vec<String>>>;

/// Logs around `next`, or vetoes with `veto`.
struct Layer {
    id: &'static str,
    log: Log,
    veto: Option<&'static str>,
}

#[async_trait]
impl ToolMiddleware for Layer {
    fn policy_id(&self) -> &str {
        self.id
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(&self, call: &ToolCall, next: Next<'_>) -> ToolResult {
        let entry = format!("{} in {}#{}", self.id, call.tool_name, call.attempt);
        self.log.lock().unwrap().push(entry);
        if let Some(reason) = self.veto {
            return Err(ToolOutputError::new(reason));
        }
        let result = next.run().await;
        self.log.lock().unwrap().push(format!("{} out", self.id));
        result
    }
}

fn agent_of(
    provider: Arc<ScriptedProvider>,
    hooks: Hooks,
    stores: Arc<Stores>,
    runs: &Runs,
) -> Agent {
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn one_call() -> Arc<ScriptedProvider> {
    Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]))
}

fn ends(envelopes: &[linch_core::Envelope]) -> Vec<ToolCallEndEvent> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => Some(end.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn layers_wrap_the_tool_first_registered_outermost() {
    let log = Log::default();
    let hooks = Hooks::default();
    for id in ["outer", "inner"] {
        let layer = Arc::new(Layer {
            id,
            log: log.clone(),
            veto: None,
        });
        let _ = hooks.tool_execute.register(layer).unwrap();
    }
    let runs = Runs::default();
    let agent = agent_of(one_call(), hooks, Arc::new(Stores::default()), &runs);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        *log.lock().unwrap(),
        [
            "outer in Read#1",
            "inner in Read#1",
            "inner out",
            "outer out"
        ]
    );
    assert_eq!(runs.lock().unwrap()["call_0_0"], 1);
    assert_eq!(ends(&envelopes)[0].result.is_error, false);
}

#[tokio::test]
async fn a_layer_that_does_not_call_next_vetoes_the_call() {
    let log = Log::default();
    let hooks = Hooks::default();
    let veto = |id, veto| {
        Arc::new(Layer {
            id,
            log: log.clone(),
            veto,
        })
    };
    let _ = hooks.tool_execute.register(veto("outer", None)).unwrap();
    let _ = hooks
        .tool_execute
        .register(veto("gate", Some("vetoed by gate")))
        .unwrap();
    let _ = hooks.tool_execute.register(veto("never", None)).unwrap();
    let runs = Runs::default();
    let provider = one_call();
    let agent = agent_of(provider.clone(), hooks, Arc::new(Stores::default()), &runs);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(runs.lock().unwrap().is_empty(), "the tool never ran");
    assert_eq!(
        *log.lock().unwrap(),
        ["outer in Read#1", "gate in Read#1", "outer out"]
    );
    let end = &ends(&envelopes)[0];
    assert!(end.result.is_error);
    let sent = &provider.requests()[1];
    let last = sent.messages.last().unwrap();
    assert_eq!(result_texts(last), ["vetoed by gate"]);
}

#[tokio::test]
async fn a_layer_does_not_run_for_a_call_waiting_for_approval() {
    let log = Log::default();
    let hooks = Hooks::default();
    let layer = Arc::new(Layer {
        id: "outer",
        log: log.clone(),
        veto: None,
    });
    let _ = hooks.tool_execute.register(layer).unwrap();
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Write"),
        text_turn("done"),
    ]));
    let agent = AgentBuilder::new(spec())
        .provider(provider)
        .tools(registry(vec![Echo::new("Write", ToolScope::Write, &runs)]))
        .permissions(policy(PermissionMode::Default))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(kinds(&envelopes).contains(&"suspended".to_owned()));
    assert!(log.lock().unwrap().is_empty());
    assert!(runs.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_pass_through_layer_changes_no_event() {
    let plain = {
        let runs = Runs::default();
        let agent = agent_of(
            one_call(),
            Hooks::default(),
            Arc::new(Stores::default()),
            &runs,
        );
        finish(agent.run("go", RunOptions::new("s1"))).await
    };
    let hooks = Hooks::default();
    let layer = Arc::new(Layer {
        id: "pass",
        log: Log::default(),
        veto: None,
    });
    let _ = hooks.tool_execute.register(layer).unwrap();
    let runs = Runs::default();
    let layered = {
        let agent = agent_of(one_call(), hooks, Arc::new(Stores::default()), &runs);
        finish(agent.run("go", RunOptions::new("s1"))).await
    };
    let shape = |envelopes: &[linch_core::Envelope]| {
        let seqs = envelopes.iter().map(|e| (e.seq, kind(&e.event)));
        let results: Vec<_> = ends(envelopes).into_iter().map(|end| end.result).collect();
        (seqs.collect::<Vec<_>>(), results)
    };
    assert_eq!(shape(&plain), shape(&layered));
}

/// A `post_tool_use` hook that answers with `response`.
fn mutating(response: PostToolUseResponse) -> Hooks {
    let hooks = Hooks::default();
    let post = hook::<PostToolUse>("rewrite", move |_| Ok(response.clone()));
    let _ = hooks.post_tool_use.register(post.arc()).unwrap();
    hooks
}

#[tokio::test]
async fn a_mutated_result_reaches_the_model_the_session_and_the_log() {
    let hooks = mutating(PostToolUseResponse::Mutate {
        content: Some(json!("scrubbed")),
        is_error: None,
    });
    let provider = one_call();
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = agent_of(provider.clone(), hooks, stores.clone(), &runs);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let sent = provider.requests()[1].messages.last().unwrap().clone();
    assert_eq!(result_texts(&sent), ["scrubbed"]);
    let stored = messages(&*stores, "s1").await;
    let tool_result = stored
        .iter()
        .find(|m| m.role == linch_core::Role::User && result_texts(m) == ["scrubbed"]);
    assert!(
        tool_result.is_some(),
        "the session holds the mutated result"
    );
    let end = &ends(&envelopes)[0];
    assert_eq!(result_texts_of(&end.result), "scrubbed");
    assert!(!end.result.is_error);
    assert_eq!(runs.lock().unwrap()["call_0_0"], 1, "the tool ran once");
}

fn result_texts_of(block: &linch_core::ToolResultBlock) -> String {
    let value = serde_json::to_value(block).unwrap();
    value["content"].as_str().map_or_else(
        || value["content"][0]["text"].as_str().unwrap().to_owned(),
        str::to_owned,
    )
}

#[tokio::test]
async fn mutating_the_error_flag_turns_a_result_into_an_error_and_back() {
    let hooks = mutating(PostToolUseResponse::Mutate {
        content: Some(json!("blocked by policy")),
        is_error: Some(true),
    });
    let provider = one_call();
    let runs = Runs::default();
    let agent = agent_of(provider.clone(), hooks, Arc::new(Stores::default()), &runs);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let end = &ends(&envelopes)[0];
    assert!(end.result.is_error);
    assert_eq!(result_texts_of(&end.result), "blocked by policy");
    let sent = provider.requests()[1].messages.last().unwrap().clone();
    assert_eq!(result_texts(&sent), ["blocked by policy"]);
}
