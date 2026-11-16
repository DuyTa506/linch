//! `Agent::cancel`: a run suspended for an approval or an answer ends
//! `aborted` through the store, with every call of its last response
//! answered so the session stays paired; a late approve or answer is
//! refused; a cancel and an approve that race leave one winner; a crash at
//! any write of the cancel finishes the abort exactly once.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use common::*;
use linch_core::ask_user::{AnswerValue, AskUser, AskUserAnswer};
use linch_core::events::kinds::ResultSubtype;
use linch_core::hooks::{ApprovalResponse, PreToolUse, PreToolUseRequest, PreToolUseResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, CallbackError, Commit, Envelope, Event, Hook, Hooks, Lease, LogEntry,
    NewRun, PermissionMode, Phase, RunError, RunId, RunOptions, RunRecord, RunStatus, RunStore,
    ScriptedProvider, ScriptedTurn, SessionId, SessionStore, StoreError, Tool, ToolRegistry,
    ToolScope,
};
use serde_json::json;
use tokio::sync::Notify;

const ABORTED: &str = "Tool call aborted";

/// Two write calls that ask for approval, an `AskUser` call, or both
/// kinds, then a text answer for each later turn.
fn approvals() -> Vec<ScriptedTurn> {
    vec![
        tool_turn(0, 2, "Write"),
        text_turn("done"),
        text_turn("next"),
    ]
}

fn ask() -> Vec<ScriptedTurn> {
    let question = json!({"questions": [{
        "id": "db", "header": "Database", "question": "Which database?",
        "options": [{"label": "Postgres", "description": "durable"},
                    {"label": "Memory", "description": "for tests"}]
    }]});
    let turn = TurnBuilder::new(MODEL)
        .text("asking")
        .tool_call("ask_0", "AskUser", &question)
        .usage(usage(10, 5))
        .build();
    vec![turn, text_turn("done"), text_turn("next")]
}

fn build<S: RunStore + SessionStore + 'static>(
    turns: Vec<ScriptedTurn>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let registry = ToolRegistry::new();
    let echo: Arc<dyn Tool> = Arc::new(Echo::new("Write", ToolScope::Write, runs));
    for tool in [echo, Arc::new(AskUser::new())] {
        let _ = registry.register(tool).unwrap();
    }
    let provider = Arc::new(ScriptedProvider::new(turns));
    agent(
        provider,
        Arc::new(registry),
        policy(PermissionMode::Default),
        stores,
    )
}

async fn suspend(agent: &Agent) -> RunId {
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let last = &envelopes.last().unwrap().event;
    assert!(matches!(last, Event::Suspended(_)), "{last:?}");
    run_id
}

fn owner(name: &str) -> RunOptions {
    let mut options = RunOptions::new("");
    options.owner = name.into();
    options
}

fn allow_all() -> HashMap<String, ApprovalResponse> {
    ["call_0_0", "call_0_1"]
        .into_iter()
        .map(|id| (id.to_owned(), ApprovalResponse::Allow))
        .collect()
}

fn postgres() -> HashMap<String, AskUserAnswer> {
    let answer = AskUserAnswer::new([("db".to_owned(), AnswerValue::One("Postgres".into()))]);
    HashMap::from([("ask_0".to_owned(), answer)])
}

fn results(events: &[Envelope]) -> Vec<ResultSubtype> {
    let subtypes = events.iter().filter_map(|envelope| match &envelope.event {
        Event::Result(result) => Some(result.subtype),
        _ => None,
    });
    subtypes.collect()
}

fn ends(events: &[Envelope]) -> Vec<(String, String, bool)> {
    let ends = events.iter().filter_map(|envelope| match &envelope.event {
        Event::ToolCallEnd(end) => {
            let text = serde_json::to_value(&end.result.content).unwrap()[0]["text"].clone();
            let text = text.as_str().unwrap_or_default().to_owned();
            Some((end.tool_use_id().to_owned(), text, end.result.is_error))
        }
        _ => None,
    });
    ends.collect()
}

/// The aborted run's durable state: status, one `aborted` result, one
/// aborted end per call, and a session whose last message answers every
/// call of the response before it.
async fn assert_aborted(stores: &Stores, run_id: &RunId, calls: &[&str], context: &str) {
    let record = stores.runs.load(NS, run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Aborted, "{context}");
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    assert_eq!(results(&events), [ResultSubtype::Aborted], "{context}");
    let want: Vec<_> = (calls.iter())
        .map(|id| (id.to_string(), ABORTED.to_owned(), true))
        .collect();
    assert_eq!(ends(&events), want, "{context}");
    let session = messages(stores, "s1").await;
    assert_eq!(session.len(), 3, "{context}: {session:?}");
    assert_eq!(
        result_texts(&session[2]),
        vec![ABORTED; calls.len()],
        "{context}"
    );
    let asked: Vec<_> = session[1].tool_uses().map(|call| call.id.clone()).collect();
    assert_eq!(asked, calls, "{context}");
}

#[tokio::test]
async fn a_cancel_ends_a_run_awaiting_approval_and_pairs_the_session() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let agent = build(approvals(), &runs, stores.clone());
    let run_id = suspend(&agent).await;

    assert!(agent.cancel(run_id.clone(), owner("x")).await.unwrap());
    assert_aborted(&stores, &run_id, &["call_0_0", "call_0_1"], "cancel").await;
    assert!(runs.lock().unwrap().is_empty(), "no tool ran");

    // A later run of the session reads every call answered.
    let next = finish(agent.run("again", RunOptions::new("s1"))).await;
    assert_eq!(results(&next), [ResultSubtype::Success]);
    let session = messages(&*stores, "s1").await;
    assert_eq!(session.len(), 5, "{session:?}");
}

#[tokio::test]
async fn a_cancel_ends_a_run_awaiting_input() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let agent = build(ask(), &runs, stores.clone());
    let run_id = suspend(&agent).await;
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    assert_eq!(status, RunStatus::AwaitingInput);

    assert!(agent.cancel(run_id.clone(), owner("x")).await.unwrap());
    assert_aborted(&stores, &run_id, &["ask_0"], "cancel").await;
}

/// After a cancel, an approve or an answer is refused as on any finished
/// run, and writes nothing; a second cancel returns `false`.
#[tokio::test]
async fn a_late_approve_or_answer_is_refused() {
    for turns in [approvals(), ask()] {
        let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
        let agent = build(turns, &runs, stores.clone());
        let run_id = suspend(&agent).await;
        assert!(agent.cancel(run_id.clone(), owner("x")).await.unwrap());
        let last_seq = stores.runs.load(NS, &run_id).await.unwrap().last_seq;

        let approve = agent.approve(run_id.clone(), allow_all(), owner("y"));
        let (_, error) = drain(approve).await;
        assert!(
            matches!(error, Some(RunError::Terminal { .. })),
            "{error:?}"
        );
        let (_, error) = drain(agent.answer(run_id.clone(), postgres(), owner("y"))).await;
        assert!(
            matches!(error, Some(RunError::Terminal { .. })),
            "{error:?}"
        );
        assert!(!agent.cancel(run_id.clone(), owner("y")).await.unwrap());
        let record = stores.runs.load(NS, &run_id).await.unwrap();
        assert_eq!(record.last_seq, last_seq, "nothing was written");
        assert!(runs.lock().unwrap().is_empty());
    }
}

/// Nothing to cancel: a finished run, a run that is not suspended, a
/// cancel token already cancelled. An unknown run is an error.
#[tokio::test]
async fn a_cancel_without_a_suspended_run_returns_false() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let agent = build(vec![text_turn("done")], &runs, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    assert!(!agent.cancel(run_id.clone(), owner("x")).await.unwrap());

    let agent = build(approvals(), &runs, stores.clone());
    let stream = agent.run("go", RunOptions::new("s2"));
    let run_id = stream.run_id().clone();
    // Dropped mid-run: `running`, at a phase no cancel takes.
    drop(stream);
    let mut stream = agent.run("go", RunOptions::new("s3"));
    let live = stream.run_id().clone();
    let _first = futures::StreamExt::next(&mut stream).await;
    let phase = stores.runs.load(NS, &live).await.unwrap().checkpoint.phase;
    assert!(
        !matches!(phase, Phase::AwaitingApproval { .. }),
        "{phase:?}"
    );
    assert!(!agent.cancel(live.clone(), owner("x")).await.unwrap());
    drop(stream);
    let error = agent.cancel(run_id, owner("x")).await.unwrap_err();
    assert!(
        matches!(error, RunError::Store(StoreError::NotFound { .. })),
        "{error:?}"
    );

    let suspended = suspend(&build(approvals(), &runs, stores.clone())).await;
    let options = owner("x");
    options.cancel.cancel();
    assert!(!agent.cancel(suspended.clone(), options).await.unwrap());
    let status = stores.runs.load(NS, &suspended).await.unwrap().status;
    assert_eq!(status, RunStatus::AwaitingApproval, "untouched");
}

/// A cancel of a run started under another contract still ends it: it
/// runs nothing of the agent's.
#[tokio::test]
async fn a_cancel_ignores_the_contract() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let run_id = suspend(&build(approvals(), &runs, stores.clone())).await;
    let mut spec = spec();
    spec.system_prompt = "Another agent.".into();
    let other = agent_with(
        spec,
        Arc::new(ScriptedProvider::new(Vec::new())),
        registry(Vec::new()),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    assert!(other.cancel(run_id.clone(), owner("x")).await.unwrap());
    assert_aborted(&stores, &run_id, &["call_0_0", "call_0_1"], "other").await;
}

/// Stores whose claims by `owner` land, then wait for `release`.
struct Gated {
    inner: Arc<Stores>,
    owner: &'static str,
    /// Hold the claim before it lands instead of after.
    before: bool,
    landed: Notify,
    release: Notify,
}

impl Gated {
    fn new(inner: Arc<Stores>, owner: &'static str) -> Arc<Self> {
        Arc::new(Self {
            inner,
            owner,
            before: false,
            landed: Notify::new(),
            release: Notify::new(),
        })
    }

    fn before(inner: Arc<Stores>, owner: &'static str) -> Arc<Self> {
        Arc::new(Self {
            inner,
            owner,
            before: true,
            landed: Notify::new(),
            release: Notify::new(),
        })
    }

    async fn hold(&self, owner: &str) {
        if owner == self.owner {
            self.landed.notify_one();
            self.release.notified().await;
        }
    }
}

#[async_trait]
impl RunStore for Gated {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        RunStore::create(&*self.inner, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        if self.before {
            self.hold(owner).await;
        }
        let lease = self.inner.claim(ns, run_id, owner).await?;
        if !self.before {
            self.hold(owner).await;
        }
        Ok(lease)
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&*self.inner, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.inner.commit(lease, commit).await
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.inner.events(ns, run_id, after).await
    }
}

#[async_trait]
impl SessionStore for Gated {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        SessionStore::create(&*self.inner, ns, session).await
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&*self.inner, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.inner.append(ns, session, expected, entries).await
    }
}

/// The approve claims first and the cancel claims last: the cancel's
/// claim fences the approve out, the cancel ends the run, and the
/// approve then finds it finished. No tool runs.
#[tokio::test]
async fn a_cancel_that_claims_last_wins_over_an_approve() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let run_id = suspend(&build(approvals(), &runs, stores.clone())).await;
    let gated = Gated::new(stores.clone(), "approver");
    let agent = build(vec![text_turn("done")], &runs, gated.clone());

    let approve = agent.approve(run_id.clone(), allow_all(), owner("approver"));
    let approving = tokio::spawn(drain(approve));
    gated.landed.notified().await;
    assert!(
        agent
            .cancel(run_id.clone(), owner("canceller"))
            .await
            .unwrap()
    );
    gated.release.notify_one();
    let (_, error) = approving.await.unwrap();
    assert!(
        matches!(error, Some(RunError::Terminal { .. })),
        "{error:?}"
    );

    assert_aborted(&stores, &run_id, &["call_0_0", "call_0_1"], "race").await;
    assert!(runs.lock().unwrap().is_empty(), "no tool ran");
}

/// The cancel claims first and the approve claims last: the approve's
/// claim fences the cancel out and the run completes; the cancel then
/// finds it finished and returns `false`. Each tool runs once.
#[tokio::test]
async fn an_approve_that_claims_last_wins_over_a_cancel() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let run_id = suspend(&build(approvals(), &runs, stores.clone())).await;
    let gated = Gated::new(stores.clone(), "canceller");
    let agent = build(vec![text_turn("done")], &runs, gated.clone());

    let cancelling = {
        let (agent, run_id) = (agent.clone(), run_id.clone());
        tokio::spawn(async move { agent.cancel(run_id, owner("canceller")).await })
    };
    gated.landed.notified().await;
    let approved = finish(agent.approve(run_id.clone(), allow_all(), owner("approver"))).await;
    assert_eq!(results(&approved), [ResultSubtype::Success]);
    gated.release.notify_one();
    assert!(!cancelling.await.unwrap().unwrap(), "the cancel lost");

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let counts = runs.lock().unwrap().clone();
    assert_eq!(
        counts,
        HashMap::from([("call_0_0".into(), 1), ("call_0_1".into(), 1)])
    );
}

/// The approve claims last while the cancel is between its claim and its
/// first write: the cancel's write is fenced (`LeaseLost`) and the approve
/// completes the run.
#[tokio::test]
async fn a_fenced_cancel_writes_nothing() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let run_id = suspend(&build(approvals(), &runs, stores.clone())).await;
    let gated = Gated::new(stores.clone(), "canceller");
    let agent = build(vec![text_turn("done")], &runs, gated.clone());

    let cancelling = {
        let (agent, run_id) = (agent.clone(), run_id.clone());
        tokio::spawn(async move { agent.cancel(run_id, owner("canceller")).await })
    };
    gated.landed.notified().await;
    // The approve claims and reaches its first tool, its run live.
    let mut approve = agent.approve(run_id.clone(), allow_all(), owner("approver"));
    loop {
        let next = futures::StreamExt::next(&mut approve)
            .await
            .unwrap()
            .unwrap();
        if matches!(next.event, Event::ToolCallStart(_)) {
            break;
        }
    }
    gated.release.notify_one();
    let error = cancelling.await.unwrap().unwrap_err();
    assert!(lease_lost(&error), "{error:?}");
    let rest = finish(approve).await;
    assert_eq!(results(&rest), [ResultSubtype::Success]);
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}

fn lease_lost(error: &RunError) -> bool {
    matches!(
        error,
        RunError::Store(StoreError::LeaseLost { .. })
            | RunError::Journal(linch_core::JournalError::Store(
                StoreError::LeaseLost { .. }
            ))
    )
}

/// A crash at any write of the cancel (its claim, the `Aborting` commit,
/// the session append, the terminal commit), before or after the write
/// lands, never undoes the cancel: an approve is refused once the intent
/// is durable, a resume or a retried cancel finishes the abort, and the
/// end state is the one a cancel without a crash leaves, with the results
/// appended once and no tool or provider call made.
#[tokio::test]
async fn a_crash_at_any_write_of_a_cancel_finishes_the_abort_once() {
    type Script = fn() -> Vec<ScriptedTurn>;
    let cases: [(Script, &[&str]); 2] = [(approvals, &["call_0_0", "call_0_1"]), (ask, &["ask_0"])];
    for (turns, calls) in cases {
        for n in 1..=4 {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                for finisher in ["cancel", "resume"] {
                    let context = format!("{calls:?}: write {n} failing {timing:?}, {finisher}");
                    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
                    let run_id = suspend(&build(turns(), &runs, stores.clone())).await;
                    let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
                    let crashing = build(turns(), &runs, faulty.clone());
                    let crashed = crashing.cancel(run_id.clone(), owner("x")).await;
                    assert!(crashed.is_err(), "{context}: {crashed:?}");
                    assert_eq!(faulty.writes(), n, "{context}");

                    // Only an empty script: a provider call would fail the run.
                    let agent = build(Vec::new(), &runs, stores.clone());
                    let record = stores.runs.load(NS, &run_id).await.unwrap();
                    let intent = matches!(record.checkpoint.phase, Phase::Aborting { .. });
                    let terminal = record.status.is_terminal();
                    if intent {
                        let approve = agent.approve(run_id.clone(), allow_all(), owner("y"));
                        let (_, error) = drain(approve).await;
                        let refused = match terminal {
                            true => matches!(error, Some(RunError::Terminal { .. })),
                            false => matches!(error, Some(RunError::NotSuspended { .. })),
                        };
                        assert!(refused, "{context}: {error:?}");
                    }
                    match finisher {
                        "cancel" => {
                            let ended = agent.cancel(run_id.clone(), owner("y")).await.unwrap();
                            assert_eq!(ended, !terminal, "{context}");
                        }
                        _ if terminal || !intent => {
                            agent.cancel(run_id.clone(), owner("y")).await.unwrap();
                        }
                        _ => {
                            let resumed = finish(agent.resume(run_id.clone(), owner("y"))).await;
                            assert_eq!(results(&resumed), [ResultSubtype::Aborted], "{context}");
                        }
                    }
                    assert_aborted(&stores, &run_id, calls, &context).await;
                    assert!(runs.lock().unwrap().is_empty(), "{context}: a tool ran");
                }
            }
        }
    }
}

/// The cancel's check passed, then an approve claimed the run and started
/// its first tool before the cancel claimed. The cancel's claim fences the
/// approve out and the cancel ends the run from the batch the approve left:
/// the started call gets its interrupted placeholder, the other an aborted
/// result. The approve's next write fails with `LeaseLost`.
#[tokio::test]
async fn a_cancel_that_claims_after_a_tool_started_ends_the_run() {
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let run_id = suspend(&build(approvals(), &runs, stores.clone())).await;
    let gated = Gated::before(stores.clone(), "canceller");
    let agent = build(vec![text_turn("done")], &runs, gated.clone());

    let cancelling = {
        let (agent, run_id) = (agent.clone(), run_id.clone());
        tokio::spawn(async move { agent.cancel(run_id, owner("canceller")).await })
    };
    gated.landed.notified().await;
    // The approve's tool start is durable; its run waits while unpolled.
    let mut approve = agent.approve(run_id.clone(), allow_all(), owner("approver"));
    loop {
        let next = futures::StreamExt::next(&mut approve).await.unwrap();
        if matches!(next.unwrap().event, Event::ToolCallStart(_)) {
            break;
        }
    }
    gated.release.notify_one();
    assert!(cancelling.await.unwrap().unwrap(), "the cancel won");
    let (_, error) = drain(approve).await;
    assert!(error.as_ref().is_some_and(lease_lost), "{error:?}");

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Aborted);
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    assert_eq!(results(&events), [ResultSubtype::Aborted]);
    let session = messages(&*stores, "s1").await;
    assert_eq!(session.len(), 3, "{session:?}");
    assert_eq!(
        result_texts(&session[2]),
        [linch_core::journal::INTERRUPTED_TOOL_TEXT, ABORTED]
    );
}

/// A `pre_tool_use` hook that never answers; it says when it was called.
struct Waits(Arc<Notify>);

#[async_trait]
impl Hook<PreToolUse> for Waits {
    fn policy_id(&self) -> &str {
        "waits"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(&self, _: &PreToolUseRequest) -> Result<PreToolUseResponse, CallbackError> {
        self.0.notify_one();
        std::future::pending().await
    }
}

/// A run cancelled while it plans its tool calls (a hook keeps it there)
/// answers each call with an aborted result before it ends, so the next
/// run of the session is paired.
#[tokio::test]
async fn a_cancel_while_planning_pairs_the_session() {
    let entered = Arc::new(Notify::new());
    let hooks = Hooks::default();
    let waits = Arc::new(Waits(Arc::clone(&entered)));
    let _ = hooks.pre_tool_use.register(waits).unwrap();
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let agent = AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new(approvals())))
        .tools(registry(vec![Echo::new("Write", ToolScope::Write, &runs)]))
        .permissions(policy(PermissionMode::Default))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    let token = options.cancel.clone();
    let canceller = tokio::spawn(async move {
        entered.notified().await;
        token.cancel();
    });
    let envelopes = finish(agent.run("go", options)).await;
    canceller.await.unwrap();
    assert_eq!(results(&envelopes), [ResultSubtype::Aborted]);
    let calls = ["call_0_0", "call_0_1"];
    assert_aborted(&stores, &RunId::new("r1"), &calls, "plan").await;

    let next = finish(agent.run("again", RunOptions::new("s1"))).await;
    assert_eq!(results(&next), [ResultSubtype::Success]);
}
