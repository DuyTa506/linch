//! `spec.tool_retry`: an idempotent tool that fails with a class the policy
//! lists runs again, each retry durable first; a tool that is not
//! idempotent never does; and a crash at every write of a retried call
//! resumes without running anything twice.

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use linch_core::events::kinds::ToolRetryEvent;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, JsonObject, PermissionMode, RunId, RunOptions,
    RunStatus, RunStore, ScriptedTurn, SessionStore, Tool, ToolContext, ToolOutput,
    ToolOutputError, ToolRegistry, ToolResult, ToolRetry, ToolScope,
};
use serde_json::json;

/// A read tool that fails its first `fail_times` attempts with `code`, and
/// counts every attempt it gets (across agents, so a resume is counted).
struct Shaky {
    idempotent: bool,
    fail_times: u32,
    code: Option<&'static str>,
    /// Sleep this long on the first attempt.
    slow_first: Option<Duration>,
    attempts: AtomicU32,
    schema: JsonObject,
}

impl Shaky {
    fn new(idempotent: bool, fail_times: u32, code: Option<&'static str>) -> Arc<Self> {
        Arc::new(Self {
            idempotent,
            fail_times,
            code,
            slow_first: None,
            attempts: AtomicU32::new(0),
            schema: json!({"type": "object"}).as_object().unwrap().clone(),
        })
    }

    fn slow_first(idempotent: bool, wait: Duration) -> Arc<Self> {
        Arc::new(Self {
            idempotent,
            fail_times: 0,
            code: None,
            slow_first: Some(wait),
            attempts: AtomicU32::new(0),
            schema: json!({"type": "object"}).as_object().unwrap().clone(),
        })
    }

    fn attempts(&self) -> u32 {
        self.attempts.load(Ordering::SeqCst)
    }
}

/// `Shaky` as a tool; the test keeps its own handle to count attempts.
struct Handle(Arc<Shaky>);

#[async_trait]
impl Tool for Handle {
    fn name(&self) -> &str {
        "Fetch"
    }
    fn description(&self) -> &str {
        "Fetch a thing."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.0.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    fn idempotent(&self) -> bool {
        self.0.idempotent
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let this = &self.0;
        let n = this.attempts.fetch_add(1, Ordering::SeqCst);
        if n == 0
            && let Some(wait) = this.slow_first
        {
            tokio::time::sleep(wait).await;
        }
        if n < this.fail_times {
            return Err(ToolOutputError {
                message: "busy".into(),
                code: this.code.map(str::to_owned),
                details: None,
            });
        }
        Ok(ToolOutput::new(format!("ok after {}", n + 1)))
    }
}

fn fast(retry: impl FnOnce(&mut ToolRetry)) -> AgentSpec {
    let mut policy = ToolRetry {
        base_delay_ms: 1,
        max_delay_ms: 2,
        ..ToolRetry::default()
    };
    retry(&mut policy);
    let mut spec = spec();
    spec.tool_retry = Some(policy);
    spec
}

fn fetch_turns() -> Vec<ScriptedTurn> {
    let call = TurnBuilder::new(MODEL)
        .text("working")
        .tool_call("call_1", "Fetch", &json!({}))
        .usage(usage(10, 5))
        .build();
    vec![call, text_turn("done")]
}

fn agent_on<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    tool: &Arc<Shaky>,
    stores: Arc<S>,
) -> (Agent, Arc<Replay>) {
    let provider = Arc::new(Replay::new(fetch_turns()));
    let registry = ToolRegistry::new();
    let _ = registry.register(Arc::new(Handle(tool.clone()))).unwrap();
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    (agent, provider)
}

fn retries(events: &[Envelope]) -> Vec<ToolRetryEvent> {
    (events.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolRetry(retry) => Some(retry.clone()),
            _ => None,
        })
        .collect()
}

fn end_error(events: &[Envelope]) -> Option<bool> {
    (events.iter()).find_map(|envelope| match &envelope.event {
        Event::ToolCallEnd(end) => Some(end.is_error()),
        _ => None,
    })
}

#[tokio::test]
async fn a_transient_failure_retries_and_then_succeeds() {
    let tool = Shaky::new(true, 2, Some("transient"));
    let (agent, provider) = agent_on(fast(|_| {}), &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(tool.attempts(), 3);
    let retried = retries(&events);
    let seen: Vec<_> = retried
        .iter()
        .map(|r| (r.attempt, r.max_attempts))
        .collect();
    assert_eq!(seen, [(2, 3), (3, 3)]);
    assert_eq!(retried[0].error_class, "transient");
    assert_eq!(retried[0].error, "busy");
    assert_eq!(retried[0].tool_use_id, "call_1");
    let order: Vec<_> = kinds(&events)
        .into_iter()
        .filter(|k| k.starts_with("tool_"))
        .collect();
    assert_eq!(
        order,
        [
            "tool_call_start",
            "tool_retry",
            "tool_retry",
            "tool_call_end"
        ]
    );
    assert_eq!(end_error(&events), Some(false));
    let sent = provider.calls();
    assert_eq!(
        sent[&1], 1,
        "the model is called once with the final result"
    );
}

#[tokio::test]
async fn a_tool_that_is_not_idempotent_is_never_retried() {
    let tool = Shaky::new(false, 1, Some("transient"));
    // Naming it in the policy does not make it safe to run twice.
    let spec = fast(|r| r.tools = Some(vec!["Fetch".into()]));
    let (agent, _) = agent_on(spec, &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(tool.attempts(), 1);
    assert!(retries(&events).is_empty());
    assert_eq!(end_error(&events), Some(true));
}

#[tokio::test]
async fn only_listed_classes_are_retried_and_attempts_run_out() {
    let tool = Shaky::new(true, 99, Some("fatal"));
    let (agent, _) = agent_on(fast(|_| {}), &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!((tool.attempts(), retries(&events).len()), (1, 0));
    assert_eq!(end_error(&events), Some(true));

    let uncoded = Shaky::new(true, 99, None);
    let (agent, _) = agent_on(fast(|_| {}), &uncoded, Arc::new(Stores::default()));
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        uncoded.attempts(),
        1,
        "an error with no class is never retried"
    );

    let always = Shaky::new(true, 99, Some("transient"));
    let (agent, _) = agent_on(
        fast(|r| r.max_attempts = 4),
        &always,
        Arc::new(Stores::default()),
    );
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!((always.attempts(), retries(&events).len()), (4, 3));
    assert_eq!(
        end_error(&events),
        Some(true),
        "the last failure is the result"
    );
}

#[tokio::test]
async fn a_call_past_the_timeout_retries_only_when_timeout_is_listed() {
    let wait = Duration::from_millis(400);
    let mut listed = fast(|r| r.retry_on = vec!["timeout".into()]);
    listed.call_timeout_ms = Some(40);
    let tool = Shaky::slow_first(true, wait);
    let (agent, _) = agent_on(listed, &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(tool.attempts(), 2);
    assert_eq!(retries(&events)[0].error_class, "timeout");
    assert_eq!(end_error(&events), Some(false));

    let mut unlisted = fast(|_| {});
    unlisted.call_timeout_ms = Some(40);
    let tool = Shaky::slow_first(true, wait);
    let (agent, _) = agent_on(unlisted, &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(tool.attempts(), 1);
    assert_eq!(end_error(&events), Some(true));
}

#[tokio::test]
async fn an_agent_without_the_policy_never_retries() {
    let tool = Shaky::new(true, 1, Some("transient"));
    let (agent, _) = agent_on(spec(), &tool, Arc::new(Stores::default()));
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(tool.attempts(), 1);
    assert!(retries(&events).is_empty());
}

#[tokio::test]
async fn a_policy_that_cannot_run_is_refused_at_build() {
    let (spec, tool) = (fast(|r| r.max_attempts = 1), Shaky::new(true, 0, None));
    let provider = Arc::new(Replay::new(fetch_turns()));
    let registry = ToolRegistry::new();
    let _ = registry.register(Arc::new(Handle(tool))).unwrap();
    let stores = Arc::new(Stores::default());
    let built = AgentBuilder::new(spec)
        .provider(provider)
        .tools(Arc::new(registry))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build();
    assert!(matches!(built, Err(linch_core::BuildError::Invalid(_))));
}

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
        }
        Err(_) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
    }
}

/// A crash at any write of a retried call, before or after the write
/// lands, never runs the tool more often than it had attempts durable: a
/// call that started and has no end is interrupted on resume, not run
/// again, so an effect is never done twice. Returns the most attempts any
/// crash point let the tool make.
async fn sweep(idempotent: bool) -> u32 {
    let probe = Shaky::new(idempotent, 1, Some("transient"));
    let clean = Arc::new(Stores::default());
    let (agent, _) = agent_on(fast(|_| {}), &probe, clean.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    let mut most = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("idempotent {idempotent}: write {n} failing {timing:?}");
            let tool = Shaky::new(idempotent, 1, Some("transient"));
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let (crashed, _) = agent_on(fast(|_| {}), &tool, faulty);
            let stream = crashed.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let (agent, _) = agent_on(fast(|_| {}), &tool, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;

            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let ends = (kinds(&events).iter())
                .filter(|kind| *kind == "tool_call_end")
                .count();
            assert_eq!(ends, 1, "{context}: the call has one durable end");
            let most_attempts = if idempotent { 2 } else { 1 };
            assert!(
                tool.attempts() <= most_attempts,
                "{context}: {}",
                tool.attempts()
            );
            let durable = retries(&events).len() as u32 + 1;
            assert!(
                tool.attempts() <= durable.max(1),
                "{context}: {} attempts, {durable} durable",
                tool.attempts()
            );
            let session = messages(&*stores, "s1").await;
            let results = (session.iter())
                .filter(|m| !result_texts(m).is_empty())
                .count();
            assert_eq!(results, 1, "{context}: the session stays paired");
            most = most.max(tool.attempts());
        }
    }
    most
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_retried_call_runs_nothing_twice() {
    assert_eq!(
        sweep(true).await,
        2,
        "a retry is reachable at some crash point"
    );
}

#[tokio::test]
async fn a_crash_at_any_write_never_retries_a_tool_that_is_not_idempotent() {
    assert_eq!(sweep(false).await, 1);
}
