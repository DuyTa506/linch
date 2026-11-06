//! Background subagents: a `Subagent` call with `run_in_background`
//! returns an acknowledgement at once; its durable child run goes on next
//! to the parent, and its end reaches the parent as a logged
//! `<task-notification>`, in the next tool results or, when the parent
//! answered with text meanwhile, as a turn of its own.

mod common;

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use common::subagents::*;
use common::*;
use linch_core::events::kinds::UserEventSubtype;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, ContentBlock, Envelope, Event, JsonObject, PermissionMode, Provider,
    ProviderError, ProviderRequest, ProviderStream, RunId, RunOptions, RunStatus, RunStore,
    ScriptedTurn, Tool, ToolContext, ToolOutput, ToolResult, ToolScope, Workers,
};
use serde_json::json;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

/// A response with one background `Subagent` call per prompt, ids
/// `sub_{i}`, then the `extra` calls.
fn background_turn(prompts: &[&str], extra: &[(&str, &str)]) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL);
    for (i, prompt) in prompts.iter().enumerate() {
        let input = json!({"description": "look", "prompt": prompt, "run_in_background": true});
        builder = builder.tool_call(&format!("sub_{i}"), "Subagent", &input);
    }
    for (id, tool) in extra {
        builder = builder.tool_call(id, tool, &json!({}));
    }
    builder.usage(usage(10, 5)).build()
}

/// [`Family`], with each child's first call held until the parent's
/// second call opens the gate.
struct Gated {
    family: Family,
    gate: Notify,
}

#[async_trait]
impl Provider for Gated {
    fn id(&self) -> &str {
        "gated"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let n = (request.messages.iter())
            .filter(|m| m.role == linch_core::Role::Assistant)
            .count();
        match (who(&request) == "parent", n) {
            (true, 1) => self.gate.notify_one(),
            (false, 0) => self.gate.notified().await,
            _ => {}
        }
        self.family.stream(request, cancel).await
    }
}

/// Waits until the child run `child` is terminal, then one more tick, so
/// the parent's stream has seen the child's stream end.
struct WaitFor {
    stores: Arc<Stores>,
    child: RunId,
    schema: JsonObject,
}

#[async_trait]
impl Tool for WaitFor {
    fn name(&self) -> &str {
        "WaitFor"
    }
    fn description(&self) -> &str {
        "Wait for the child."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        loop {
            let record = self.stores.runs.load(NS, &self.child).await;
            if record.is_ok_and(|record| record.status.is_terminal()) {
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "the child never ended"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
        Ok(ToolOutput::new("waited"))
    }
}

fn notification(id: &str, status: &str, name: &str, result: &str) -> String {
    format!(
        "<task-notification><task-id>{id}</task-id><status>{status}</status>\
         <summary>Worker '{name}' finished.</summary><result>{result}</result>\
         </task-notification>"
    )
}

fn ack(id: &str) -> String {
    format!(
        "Worker '{id}' started in background. You will receive a <task-notification> when it \
         finishes."
    )
}

fn markers(envelopes: &[Envelope]) -> Vec<(RunId, RunStatus)> {
    let markers = envelopes.iter().filter_map(|e| match &e.event {
        Event::ChildRunStatus(marker) => Some((marker.child_run_id.clone(), marker.status)),
        _ => None,
    });
    markers.collect()
}

fn builder(
    provider: Arc<dyn Provider>,
    stores: &Arc<Stores>,
    tools: Vec<Arc<dyn Tool>>,
) -> AgentBuilder {
    let registry = linch_core::ToolRegistry::new();
    for tool in tools {
        let _ = registry.register(tool).unwrap();
    }
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(Vec::new())
}

#[tokio::test]
async fn a_text_answer_waits_for_the_background_child_and_hears_of_it() {
    let parent = vec![
        background_turn(&["check x"], &[]),
        text_turn("waiting"),
        text_turn("done"),
    ];
    let family = Family::new(parent, vec![text_turn("x holds")]);
    let provider = Arc::new(Gated {
        family,
        gate: Notify::new(),
    });
    let stores = Arc::new(Stores::default());
    let workers = Workers {
        background: true,
        retain: false,
    };
    let agent = builder(provider.clone(), &stores, Vec::new())
        .workers(workers)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let child = format!("{run_id}:sub_0");
    let log = messages(&*stores, "s1").await;
    let texts: Vec<String> = log.iter().map(|m| m.text()).collect();
    assert_eq!(texts[3], "waiting");
    assert_eq!(texts[5], "done");
    assert_eq!(result_texts(&log[2]), [ack(&child)]);
    let note = notification(&child, "completed", "Agent #1", "x holds");
    assert_eq!(log[4].content, [ContentBlock::text(note.clone())]);
    let last_user = |r: &ProviderRequest| r.messages.last().unwrap().text();
    let requests = provider.family.requests_of("parent");
    assert_eq!(last_user(&requests[2]), note, "the model heard of it");
    let note_event = envelopes.iter().find_map(|e| match &e.event {
        Event::User(user) if user.message == log[4] => Some(user.subtype),
        _ => None,
    });
    assert_eq!(note_event, Some(UserEventSubtype::Notification));

    let child = RunId::new(child);
    let record = stores.runs.load(NS, &child).await.unwrap();
    assert_eq!(record.parent_run_id, Some(run_id.clone()));
    assert_eq!(record.status, RunStatus::Completed);
    let expected = [
        (child.clone(), RunStatus::Running),
        (child.clone(), RunStatus::Completed),
    ];
    assert_eq!(markers(&envelopes), expected);
    let wrapped = (envelopes.iter())
        .filter(|e| matches!(&e.event, Event::Subagent(sub) if sub.child_run_id == child))
        .count();
    assert!(wrapped > 0, "the child's events are in the parent's log");
}

#[tokio::test]
async fn a_background_child_that_ends_in_a_batch_joins_its_results() {
    let parent = vec![
        background_turn(&["check x"], &[("wait_0", "WaitFor")]),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, vec![text_turn("x holds")]));
    let stores = Arc::new(Stores::default());
    let run_id = RunId::new("r1");
    let wait = WaitFor {
        stores: stores.clone(),
        child: RunId::new("r1:sub_0"),
        schema: object(json!({"type": "object"})),
    };
    let workers = Workers {
        background: true,
        retain: false,
    };
    let agent = builder(family.clone(), &stores, vec![Arc::new(wait)])
        .workers(workers)
        .build()
        .unwrap();
    let mut options = RunOptions::new("s1");
    options.run_id = Some(run_id.clone());
    finish(agent.run("go", options)).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(
        log.len(),
        4,
        "prompt, calls, results, answer: no waiting turn"
    );
    assert_eq!(result_texts(&log[2]), [ack("r1:sub_0"), "waited".into()]);
    let note = notification("r1:sub_0", "completed", "Agent #1", "x holds");
    assert_eq!(log[2].content[2], ContentBlock::text(note));
}

#[tokio::test]
async fn a_background_call_without_background_workers_is_refused() {
    let parent = vec![background_turn(&["check x"], &[]), text_turn("done")];
    let family = Arc::new(Family::new(parent, Vec::new()));
    let stores = Arc::new(Stores::default());
    let agent = builder(family.clone(), &stores, Vec::new())
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        [
            "Background subagents are not enabled for this agent. Run this Subagent without \
          run_in_background."
        ]
    );
    assert!(family.requests_of("check x").is_empty(), "no child ran");
}

/// Runs until its call is cancelled, after telling `started`.
struct Block(Arc<Notify>, JsonObject);

#[async_trait]
impl Tool for Block {
    fn name(&self) -> &str {
        "Block"
    }
    fn description(&self) -> &str {
        "Block."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.1
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        self.0.notify_one();
        ctx.cancel.cancelled().await;
        Ok(ToolOutput::new("stopped"))
    }
}

#[tokio::test]
async fn a_parent_cancelled_while_it_waits_aborts_its_background_child() {
    let parent = vec![background_turn(&["check x"], &[]), text_turn("waiting")];
    let family = Arc::new(Family::new(parent, vec![tool_turn(0, 1, "Block")]));
    let stores = Arc::new(Stores::default());
    let started = Arc::new(Notify::new());
    let block = Block(started.clone(), object(json!({"type": "object"})));
    let workers = Workers {
        background: true,
        retain: false,
    };
    let agent = builder(family.clone(), &stores, vec![Arc::new(block)])
        .workers(workers)
        .build()
        .unwrap();
    let cancel = CancellationToken::new();
    let mut options = RunOptions::new("s1");
    options.cancel = cancel.clone();
    let stream = agent.run("go", options);
    let run_id = stream.run_id().clone();
    let canceller = tokio::spawn(async move {
        started.notified().await;
        cancel.cancel();
    });
    let envelopes = finish(stream).await;
    canceller.await.unwrap();

    let ended = |envelopes: &[Envelope]| {
        envelopes.iter().find_map(|e| match &e.event {
            Event::Result(result) => Some(result.subtype),
            _ => None,
        })
    };
    use linch_core::events::kinds::ResultSubtype::Aborted;
    assert_eq!(ended(&envelopes), Some(Aborted));
    let child = RunId::new(format!("{run_id}:sub_0"));
    let record = stores.runs.load(NS, &child).await.unwrap();
    assert_eq!(record.status, RunStatus::Aborted);
    let child_events = stores.runs.events(NS, &child, 0).await.unwrap();
    assert_eq!(ended(&child_events), Some(Aborted));
    // The cancel may land before or after the text answer; either way the
    // aborting parent logs no notification of the child it stopped.
    let log = messages(&*stores, "s1").await;
    assert!(
        log.iter()
            .all(|m| !m.text().contains("<task-notification>"))
    );
}
