//! Steering a run: text sent with `Agent::steer` is a `steer_received` event
//! in the run's journal, durable before the caller is answered, and the run
//! logs it as a user message of its own after a whole tool batch (or before
//! a text answer would end the run). A resumed run reads what is pending back
//! from its journal, so a steer is logged once whatever write a crash cuts.

mod common;

use std::sync::{Arc, Mutex, OnceLock};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::events::kinds::UserEventSubtype;
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::{ProviderError, ProviderEvent, ProviderRequest, ProviderStream};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, ContentBlock, Envelope, Event, JsonObject, MAX_PENDING_STEERS,
    MAX_STEER_BYTES, Message, PermissionMode, Provider, Role, RunError, RunId, RunOptions,
    RunStatus, RunStore, ScriptedProvider, SessionStore, Tool, ToolContext, ToolOutput,
    ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

const TEXT: &str = "use tabs, not spaces";

type Sent = Arc<Mutex<Vec<JoinHandle<Result<String, RunError>>>>>;

/// Steers the run it belongs to without waiting for the answer, which only
/// comes once the run takes its next step.
fn send(agent: &Agent, run_id: &RunId, id: &str, sent: &Sent) {
    let (agent, run_id, id) = (agent.clone(), run_id.clone(), id.to_owned());
    let handle = tokio::spawn(async move { agent.steer(run_id, TEXT, Some(id)).await });
    sent.lock().unwrap().push(handle);
}

/// A tool that steers its own run, as a host would meanwhile.
struct Steerer {
    agent: Arc<OnceLock<Agent>>,
    sent: Sent,
    schema: JsonObject,
}

#[async_trait]
impl Tool for Steerer {
    fn name(&self) -> &str {
        "Steerer"
    }
    fn description(&self) -> &str {
        "Steer the run."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        let agent = self.agent.get().expect("the agent is set");
        send(agent, &ctx.run_id, "s1", &self.sent);
        // Let the spawned call reach the run's inbox.
        tokio::task::yield_now().await;
        Ok(ToolOutput::new("steered"))
    }
}

fn steerer(agent: &Arc<OnceLock<Agent>>, sent: &Sent) -> Arc<ToolRegistry> {
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let tool = Steerer {
        agent: Arc::clone(agent),
        sent: Arc::clone(sent),
        schema,
    };
    let tools = ToolRegistry::new();
    let _ = tools.register(Arc::new(tool)).unwrap();
    Arc::new(tools)
}

/// A provider that steers the run the first time it is asked, like a host
/// steering during the model call, then answers as `Replay`.
struct Steering {
    inner: Replay,
    agent: Arc<OnceLock<Agent>>,
    sent: Sent,
}

#[async_trait]
impl Provider for Steering {
    fn id(&self) -> &str {
        "steering"
    }
    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if request.messages.len() == 1 {
            let agent = self.agent.get().expect("the agent is set");
            send(agent, &RunId::new("r1"), "s1", &self.sent);
            tokio::task::yield_now().await;
        }
        let stream: BoxStream<'static, Result<ProviderEvent, ProviderError>> =
            self.inner.stream(request, cancel).await?.boxed();
        Ok(stream)
    }
}

fn agent_on(
    provider: Arc<dyn Provider>,
    tools: Arc<ToolRegistry>,
    mode: PermissionMode,
    stores: Arc<impl RunStore + SessionStore + 'static>,
) -> Agent {
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(mode))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn options() -> RunOptions {
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    options
}

fn steer_message() -> Message {
    Message::new(Role::User, vec![ContentBlock::text(TEXT)])
}

/// `(id, message)` of each alignment event.
fn alignments(events: &[Envelope]) -> Vec<(String, Message)> {
    let mut found = Vec::new();
    for envelope in events {
        if let Event::User(user) = &envelope.event
            && user.subtype == UserEventSubtype::Alignment
        {
            let id = user.steer_id.clone().expect("an alignment names its steer");
            found.push((id, user.message.clone()));
        }
    }
    found
}

fn received(events: &[Envelope]) -> usize {
    let steers = events
        .iter()
        .filter(|e| matches!(e.event, Event::SteerReceived(_)));
    steers.count()
}

#[tokio::test]
async fn a_steer_sent_during_a_batch_is_its_own_message_after_the_results() {
    let stores = Arc::new(Stores::default());
    let (cell, sent) = (Arc::new(OnceLock::new()), Sent::default());
    let turns = vec![tool_turn(0, 1, "Steerer"), text_turn("done")];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let agent = agent_on(
        provider.clone(),
        steerer(&cell, &sent),
        PermissionMode::SkipDangerous,
        stores.clone(),
    );
    cell.set(agent.clone()).ok();
    let envelopes = finish(agent.run("go", options())).await;
    let task = sent.lock().unwrap().remove(0);
    let id = task.await.unwrap().unwrap();
    assert_eq!(id, "s1");

    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 5, "prompt, call, results, steer, answer");
    assert_eq!(result_texts(&log[2]), ["steered"]);
    assert_eq!(log[2].content.len(), 1, "the results are untouched");
    assert_eq!(log[3], steer_message());
    let second = &provider.requests()[1];
    assert_eq!(second.messages.last(), Some(&log[3]), "and sent");
    let events = stores.runs.events(NS, &RunId::new("r1"), 0).await.unwrap();
    assert_eq!(received(&events), 1);
    assert_eq!(alignments(&envelopes), [("s1".to_owned(), steer_message())]);
}

#[tokio::test]
async fn a_steer_pending_at_a_text_answer_is_read_in_another_turn() {
    let stores = Arc::new(Stores::default());
    let (cell, sent) = (Arc::new(OnceLock::new()), Sent::default());
    let provider = Arc::new(Steering {
        inner: Replay::new(vec![text_turn("first"), text_turn("second")]),
        agent: Arc::clone(&cell),
        sent: Arc::clone(&sent),
    });
    let tools = Arc::new(ToolRegistry::new());
    let agent = agent_on(
        provider,
        tools,
        PermissionMode::SkipDangerous,
        stores.clone(),
    );
    cell.set(agent.clone()).ok();
    let envelopes = finish(agent.run("go", options())).await;
    let task = sent.lock().unwrap().remove(0);
    task.await.unwrap().unwrap();

    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 4, "prompt, first, steer, second");
    assert_eq!(log[2], steer_message());
    let result = envelopes.last().unwrap();
    assert!(matches!(&result.event, Event::Result(r) if r.final_text.as_deref() == Some("second")));
}

#[tokio::test]
async fn a_run_nobody_drives_and_that_is_not_waiting_refuses_a_steer() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let tools = Arc::new(ToolRegistry::new());
    let agent = agent_on(
        provider,
        tools,
        PermissionMode::SkipDangerous,
        stores.clone(),
    );
    // The stream is dropped after its first event: the run is left `running`
    // with no runner.
    let mut stream = agent.run("go", options());
    stream.next().await.unwrap().unwrap();
    drop(stream);
    tokio::task::yield_now().await;
    let run_id = RunId::new("r1");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Running);
    let error = agent.steer(run_id.clone(), TEXT, None).await.unwrap_err();
    assert!(matches!(error, RunError::InvalidSteer { .. }), "{error}");
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    assert_eq!(received(&events), 0, "nothing was written");
}

/// A run suspended on a write's approval, and the agent that drives it.
async fn suspended(stores: &Arc<Stores>) -> Agent {
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
    let turns = vec![tool_turn(0, 1, "Write"), text_turn("done")];
    let provider = Arc::new(Replay::new(turns));
    let agent = agent_on(provider, tools, PermissionMode::Default, stores.clone());
    finish(agent.run("go", options())).await;
    let status = stores
        .runs
        .load(NS, &RunId::new("r1"))
        .await
        .unwrap()
        .status;
    assert_eq!(status, RunStatus::AwaitingApproval);
    agent
}

fn allow() -> std::collections::HashMap<String, ApprovalResponse> {
    [("call_0_0".to_owned(), ApprovalResponse::Allow)].into()
}

#[tokio::test]
async fn a_suspended_run_takes_a_steer_and_delivers_it_once_it_continues() {
    let stores = Arc::new(Stores::default());
    let agent = suspended(&stores).await;
    let run_id = RunId::new("r1");
    assert_eq!(
        agent
            .steer(run_id.clone(), TEXT, Some("a1".into()))
            .await
            .unwrap(),
        "a1"
    );
    let again = agent.steer(run_id.clone(), TEXT, Some("a1".into())).await;
    assert_eq!(again.unwrap(), "a1", "a resent id is taken once");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(
        record.status,
        RunStatus::AwaitingApproval,
        "still suspended"
    );

    let blank = agent.steer(run_id.clone(), "  ", None).await;
    assert!(matches!(blank, Err(RunError::InvalidSteer { .. })));
    let long = agent
        .steer(run_id.clone(), "x".repeat(MAX_STEER_BYTES + 1), None)
        .await;
    assert!(matches!(long, Err(RunError::InvalidSteer { .. })));
    let bad = agent.steer(run_id.clone(), TEXT, Some("a b".into())).await;
    assert!(matches!(bad, Err(RunError::InvalidSteer { .. })));
    let unknown = agent.steer(RunId::new("nope"), TEXT, None).await;
    assert!(matches!(
        unknown,
        Err(RunError::Store(StoreError::NotFound { .. }))
    ));
    for n in 1..MAX_PENDING_STEERS {
        agent
            .steer(run_id.clone(), TEXT, Some(format!("f{n}")))
            .await
            .unwrap();
    }
    let full = agent.steer(run_id.clone(), TEXT, None).await;
    assert!(
        matches!(full, Err(RunError::SteerQueueFull { .. })),
        "{full:?}"
    );

    let envelopes = finish(agent.approve(run_id.clone(), allow(), RunOptions::new("s1"))).await;
    let delivered = alignments(&envelopes);
    assert_eq!(delivered.len(), MAX_PENDING_STEERS);
    assert_eq!(delivered[0].0, "a1", "in the order they came");
    let log = messages(&*stores, "s1").await;
    let steers = log
        .iter()
        .filter(|message| **message == steer_message())
        .count();
    assert_eq!(steers, MAX_PENDING_STEERS, "each is a message of its own");
    assert_eq!(log.last().unwrap().role, Role::Assistant, "then the answer");

    let late = agent.steer(run_id, TEXT, None).await;
    assert!(matches!(late, Err(RunError::Terminal { .. })), "{late:?}");
}

use linch_core::StoreError;

/// Everything a clean continuation of the suspended run writes.
async fn continuation_writes() -> u64 {
    let stores = Arc::new(Stores::default());
    let agent = suspended(&stores).await;
    agent
        .steer(RunId::new("r1"), TEXT, Some("s1".into()))
        .await
        .unwrap();
    let before = stores.log.lock().unwrap().len();
    finish(agent.approve(RunId::new("r1"), allow(), RunOptions::new("s1"))).await;
    (stores.log.lock().unwrap().len() - before) as u64
}

/// Continue the run the way a client does after a stop: approve what waits,
/// resume what runs, until it ends.
async fn recover(agent: &Agent, stores: &Stores) {
    for _ in 0..4 {
        let record = stores.runs.load(NS, &RunId::new("r1")).await.unwrap();
        let stream = match record.status {
            status if status.is_terminal() => return,
            RunStatus::AwaitingApproval => {
                agent.approve(RunId::new("r1"), allow(), RunOptions::new("s1"))
            }
            _ => agent.resume(RunId::new("r1"), RunOptions::new("s1")),
        };
        let _ = drain(stream).await;
    }
}

/// The steer is in the log once, named once, and the run finished.
async fn assert_delivered_once(stores: &Stores, context: &str) {
    let log = messages(stores, "s1").await;
    let steers = log
        .iter()
        .filter(|message| **message == steer_message())
        .count();
    assert_eq!(steers, 1, "{context}: logged once");
    let events = stores.runs.events(NS, &RunId::new("r1"), 0).await.unwrap();
    assert_eq!(alignments(&events).len(), 1, "{context}: named once");
    assert_eq!(received(&events), 1, "{context}: received once");
    let record = stores.runs.load(NS, &RunId::new("r1")).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed, "{context}");
}

#[tokio::test]
async fn a_crash_at_any_write_of_the_continuation_delivers_the_steer_once() {
    let total = continuation_writes().await;
    assert!(total > 5, "{total} writes");
    for n in 1..=total {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("continuation write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let agent = suspended(&stores).await;
            agent
                .steer(RunId::new("r1"), TEXT, Some("s1".into()))
                .await
                .unwrap();
            let runs = Runs::default();
            let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
            let provider = Arc::new(Replay::new(vec![
                tool_turn(0, 1, "Write"),
                text_turn("done"),
            ]));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashing = agent_on(provider.clone(), tools, PermissionMode::Default, faulty);
            let approved = crashing.approve(RunId::new("r1"), allow(), RunOptions::new("s1"));
            let (_, error) = drain(approved).await;
            assert!(error.is_some(), "{context}: the run must stop");
            let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
            let clean = agent_on(provider, tools, PermissionMode::Default, stores.clone());
            recover(&clean, &stores).await;
            assert_delivered_once(&stores, &context).await;
        }
    }
}

#[tokio::test]
async fn a_crash_at_any_write_of_the_steer_still_delivers_it_once() {
    for n in 1..=3 {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("steer write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let agent = suspended(&stores).await;
            let runs = Runs::default();
            let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
            let provider = Arc::new(Replay::new(vec![
                tool_turn(0, 1, "Write"),
                text_turn("done"),
            ]));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashing = agent_on(provider, tools, PermissionMode::Default, faulty);
            let first = crashing
                .steer(RunId::new("r1"), TEXT, Some("s1".into()))
                .await;
            // The same call sent again after a failure is what a client does.
            let second = agent.steer(RunId::new("r1"), TEXT, Some("s1".into())).await;
            match second {
                Ok(id) => {
                    assert_eq!(id, "s1", "{context}: {first:?}");
                    recover(&agent, &stores).await;
                    assert_delivered_once(&stores, &context).await;
                }
                // The failed write left the claim's `running` status: the run
                // is idle until it is resumed, and the steer is refused. If
                // its event reached the journal first, the resume delivers it
                // once; if not, the caller sends it again after the resume.
                Err(RunError::InvalidSteer { .. }) => {
                    recover(&agent, &stores).await;
                    let log = messages(&*stores, "s1").await;
                    let logged = log.iter().filter(|m| **m == steer_message()).count();
                    let events = stores.runs.events(NS, &RunId::new("r1"), 0).await.unwrap();
                    assert_eq!(logged, received(&events), "{context}: logged as received");
                    assert!(logged <= 1, "{context}: at most once");
                }
                Err(other) => panic!("{context}: {other:?}"),
            }
        }
    }
}

/// A steer sent during a tool batch of a running run: cut the run at every
/// write, either side of it, and continue it. A steer whose call was
/// answered is delivered once; one whose call failed, at most once.
#[tokio::test]
async fn a_crash_at_any_write_of_a_running_run_never_loses_an_answered_steer() {
    let baseline = {
        let stores = Arc::new(Stores::default());
        let (cell, sent) = (Arc::new(OnceLock::new()), Sent::default());
        let provider = Arc::new(Replay::new(vec![
            tool_turn(0, 1, "Steerer"),
            text_turn("done"),
        ]));
        let tools = steerer(&cell, &sent);
        let agent = agent_on(
            provider,
            tools,
            PermissionMode::SkipDangerous,
            stores.clone(),
        );
        cell.set(agent.clone()).ok();
        finish(agent.run("go", options())).await;
        let writes = stores.log.lock().unwrap().len();
        writes as u64
    };
    for n in 1..=baseline {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let (cell, sent) = (Arc::new(OnceLock::new()), Sent::default());
            let provider = Arc::new(Replay::new(vec![
                tool_turn(0, 1, "Steerer"),
                text_turn("done"),
            ]));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let tools = steerer(&cell, &sent);
            let crashing = agent_on(
                provider.clone(),
                tools,
                PermissionMode::SkipDangerous,
                faulty,
            );
            cell.set(crashing.clone()).ok();
            let (_, error) = drain(crashing.run("go", options())).await;
            assert!(error.is_some(), "{context}: the run must stop");
            let (cell, again) = (Arc::new(OnceLock::new()), Sent::default());
            let tools = steerer(&cell, &again);
            let clean = agent_on(
                provider,
                tools,
                PermissionMode::SkipDangerous,
                stores.clone(),
            );
            cell.set(clean.clone()).ok();
            let run_id = RunId::new("r1");
            match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => {
                    finish(clean.resume(run_id.clone(), RunOptions::new("s1"))).await;
                }
                Err(_) => {
                    finish(clean.run("go", options())).await;
                }
            }
            // Calls the crashed run took part in, answered or not.
            let handles: Vec<_> = sent.lock().unwrap().drain(..).collect();
            let mut answered = false;
            for handle in handles {
                answered |= handle.await.unwrap().is_ok();
            }
            let log = messages(&*stores, "s1").await;
            let steers = log
                .iter()
                .filter(|message| **message == steer_message())
                .count();
            assert!(steers <= 1, "{context}: logged {steers} times");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            assert!(alignments(&events).len() <= 1, "{context}");
            if answered {
                assert_eq!(steers, 1, "{context}: an answered steer is not lost");
            }
        }
    }
}
