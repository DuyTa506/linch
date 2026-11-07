//! Suspend on a permission ask, then approve or deny, on the scripted
//! provider and memory stores.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::hooks::ApprovalResponse;
use linch_core::permissions::{Rule, RuleDecision, ToolRule};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, Envelope, Event, PermissionEngine, PermissionLayer, PermissionMode, PermissionPolicy,
    Phase, RunError, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider, ToolScope,
};

struct Fixture {
    stores: Arc<Stores>,
    runs: Runs,
    provider: Arc<ScriptedProvider>,
}

impl Fixture {
    /// `k` write calls that ask for approval, then a text answer.
    fn new(k: usize) -> Self {
        Self {
            stores: Arc::new(Stores::default()),
            runs: Runs::default(),
            provider: Arc::new(ScriptedProvider::new(script(1, k, "Write"))),
        }
    }

    fn agent(&self, permissions: PermissionEngine) -> Agent {
        let tools = registry(vec![Echo::new("Write", ToolScope::Write, &self.runs)]);
        agent(
            self.provider.clone(),
            tools,
            permissions,
            self.stores.clone(),
        )
    }

    /// Start the run; it must suspend.
    async fn suspend(&self) -> (RunId, Vec<Envelope>) {
        let stream = self
            .agent(policy(PermissionMode::Default))
            .run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        (run_id, finish(stream).await)
    }

    async fn status(&self, run_id: &RunId) -> RunStatus {
        self.stores.runs.load(NS, run_id).await.unwrap().status
    }

    async fn results(&self) -> Vec<String> {
        result_texts(&messages(&*self.stores, "s1").await[2])
    }
}

fn answers(pairs: &[(&str, ApprovalResponse)]) -> HashMap<String, ApprovalResponse> {
    pairs
        .iter()
        .map(|(id, a)| (id.to_string(), a.clone()))
        .collect()
}

fn pending(envelopes: &[Envelope]) -> Vec<String> {
    match &envelopes.last().unwrap().event {
        Event::Suspended(suspended) => suspended.pending_request_ids.clone(),
        other => panic!("expected a suspended stream end, got {other:?}"),
    }
}

fn subtype(envelopes: &[Envelope]) -> ResultSubtype {
    match &envelopes.last().unwrap().event {
        Event::Result(result) => result.subtype,
        other => panic!("expected a result, got {other:?}"),
    }
}

#[tokio::test]
async fn an_ask_suspends_the_run_without_running_the_tool() {
    let fixture = Fixture::new(1);
    let (run_id, envelopes) = fixture.suspend().await;
    let tail = &kinds(&envelopes)[envelopes.len() - 2..];
    assert_eq!(tail, ["permission_request", "suspended"]);
    assert_eq!(pending(&envelopes), ["call_0_0"]);
    assert_eq!(fixture.status(&run_id).await, RunStatus::AwaitingApproval);
    assert!(fixture.runs.lock().unwrap().is_empty());
}

#[tokio::test]
async fn approving_runs_the_call_and_the_run_completes() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let agent = fixture.agent(policy(PermissionMode::Default));
    let allow = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    let envelopes = finish(agent.approve(run_id.clone(), allow, RunOptions::new("s1"))).await;
    assert_eq!(subtype(&envelopes), ResultSubtype::Success);
    assert_eq!(fixture.results().await, ["v0.0"]);
    assert_eq!(fixture.runs.lock().unwrap()["call_0_0"], 1);
    assert_eq!(fixture.status(&run_id).await, RunStatus::Completed);
}

#[tokio::test]
async fn denying_gives_the_model_a_denied_result() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let agent = fixture.agent(policy(PermissionMode::Default));
    let deny = ApprovalResponse::Deny {
        reason: "not today".into(),
    };
    let envelopes = finish(agent.approve(
        run_id,
        answers(&[("call_0_0", deny)]),
        RunOptions::new("s1"),
    ))
    .await;
    assert_eq!(subtype(&envelopes), ResultSubtype::Success);
    assert_eq!(fixture.results().await, ["Tool call denied: not today"]);
    assert!(fixture.runs.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_recorded_allow_never_beats_a_fresh_deny() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    // The approving agent's policy now denies the tool outright. The
    // permission policy is not part of the run contract.
    let mut strict = PermissionPolicy::new(PermissionMode::Default, "/");
    let rule = ToolRule::new("Write", RuleDecision::Deny);
    strict.layers = vec![PermissionLayer::new("runtime", [Rule::Tool(rule)])];
    let agent = fixture.agent(PermissionEngine::new(strict).unwrap());
    let allow = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    finish(agent.approve(run_id, allow, RunOptions::new("s1"))).await;
    let results = fixture.results().await;
    assert!(results[0].starts_with("Tool call denied"), "{results:?}");
    assert!(fixture.runs.lock().unwrap().is_empty());
}

#[tokio::test]
async fn answering_one_of_two_asks_suspends_again_for_the_other_only() {
    let fixture = Fixture::new(2);
    let (run_id, envelopes) = fixture.suspend().await;
    assert_eq!(pending(&envelopes), ["call_0_0", "call_0_1"]);
    let agent = fixture.agent(policy(PermissionMode::Default));
    let first = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    let envelopes = finish(agent.approve(run_id.clone(), first, RunOptions::new("s1"))).await;
    assert_eq!(pending(&envelopes), ["call_0_1"]);
    assert!(
        fixture.runs.lock().unwrap().is_empty(),
        "nothing runs while one call waits"
    );
    let second = answers(&[("call_0_1", ApprovalResponse::Allow)]);
    let envelopes = finish(agent.approve(run_id, second, RunOptions::new("s1"))).await;
    assert_eq!(subtype(&envelopes), ResultSubtype::Success);
    assert_eq!(fixture.results().await, ["v0.0", "v0.1"]);
    let runs = fixture.runs.lock().unwrap();
    assert_eq!((runs["call_0_0"], runs["call_0_1"]), (1, 1));
}

#[tokio::test]
async fn resuming_a_suspended_run_without_answers_suspends_it_again() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let agent = fixture.agent(policy(PermissionMode::Default));
    let envelopes = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert_eq!(pending(&envelopes), ["call_0_0"]);
    assert_eq!(fixture.status(&run_id).await, RunStatus::AwaitingApproval);
}

#[tokio::test]
async fn approve_needs_a_suspended_run_and_nothing_continues_a_terminal_one() {
    let fixture = Fixture::new(1);
    let agent = fixture.agent(policy(PermissionMode::SkipDangerous));
    // Drop a fresh run's stream after its first envelope: running, not
    // suspended.
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let first = futures::StreamExt::next(&mut stream)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(first.event, Event::Init(_)));
    drop(stream);
    let allow = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    let (_, error) =
        drain(agent.approve(run_id.clone(), allow.clone(), RunOptions::new("s1"))).await;
    assert!(
        matches!(error, Some(RunError::NotSuspended { .. })),
        "{error:?}"
    );

    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert_eq!(fixture.status(&run_id).await, RunStatus::Completed);
    for stream in [
        agent.resume(run_id.clone(), RunOptions::new("s1")),
        agent.approve(run_id.clone(), allow, RunOptions::new("s1")),
    ] {
        let (envelopes, error) = drain(stream).await;
        assert!(envelopes.is_empty());
        let terminal = matches!(
            error,
            Some(RunError::Terminal {
                status: RunStatus::Completed,
                ..
            })
        );
        assert!(terminal, "{error:?}");
    }
}

#[tokio::test]
async fn an_agent_with_another_contract_cannot_continue_the_run() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let mut other = spec();
    other.system_prompt = "A different agent.".into();
    let tools = registry(vec![Echo::new("Write", ToolScope::Write, &fixture.runs)]);
    let permissions = policy(PermissionMode::Default);
    let agent = agent_with(
        other,
        fixture.provider.clone(),
        tools,
        permissions,
        fixture.stores.clone(),
    );
    let allow = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    let epoch = fixture.stores.runs.load(NS, &run_id).await.unwrap().epoch;
    for stream in [
        agent.resume(run_id.clone(), RunOptions::new("s1")),
        agent.approve(run_id.clone(), allow, RunOptions::new("s1")),
    ] {
        let (_, error) = drain(stream).await;
        assert!(
            matches!(error, Some(RunError::ContractMismatch { .. })),
            "{error:?}"
        );
    }
    // Refused before the claim: the lease of whoever owns the run holds.
    let record = fixture.stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.epoch, epoch);
    assert!(fixture.runs.lock().unwrap().is_empty());
}

/// R1-rpc 5: a tool's scope decides approval and parallelism, so the same
/// tool declared `read` instead of `write` is another contract.
#[tokio::test]
async fn a_tool_of_another_scope_is_another_contract() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let tools = registry(vec![Echo::new("Write", ToolScope::Read, &fixture.runs)]);
    let permissions = policy(PermissionMode::Default);
    let agent = agent(
        fixture.provider.clone(),
        tools,
        permissions,
        fixture.stores.clone(),
    );
    let (_, error) = drain(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert!(
        matches!(error, Some(RunError::ContractMismatch { .. })),
        "{error:?}"
    );
    assert!(fixture.runs.lock().unwrap().is_empty(), "nothing ran");
}

/// R2-core 1: an approve that failed after its answers were committed
/// leaves a `ToolBatch` run, which may still have a live owner. Another
/// approve of it (the same one retried, or one with no answers) gets
/// `NotSuspended` before any claim, so it never fences that owner. The host
/// resumes the run instead, and the call runs once.
#[tokio::test]
async fn an_approve_after_its_answers_were_committed_is_refused_and_resume_continues() {
    let fixture = Fixture::new(1);
    let (run_id, _) = fixture.suspend().await;
    let allow = answers(&[("call_0_0", ApprovalResponse::Allow)]);
    // The approve writes: claim (1), the answers with the batch checkpoint
    // (2), the call's start (3). Fail before 3: the answers are durable and
    // the call never started.
    let faulty = Arc::new(Faulty::new(fixture.stores.clone(), 3, FaultTiming::Before));
    let tools = registry(vec![Echo::new("Write", ToolScope::Write, &fixture.runs)]);
    let crashing = agent(
        fixture.provider.clone(),
        tools,
        policy(PermissionMode::Default),
        faulty,
    );
    let options = RunOptions::new("s1");
    let (_, error) = drain(crashing.approve(run_id.clone(), allow.clone(), options)).await;
    assert!(error.is_some(), "the approve must fail");
    let record = fixture.stores.runs.load(NS, &run_id).await.unwrap();
    let phase = &record.checkpoint.phase;
    assert!(matches!(phase, Phase::ToolBatch { .. }), "{phase:?}");
    assert!(fixture.runs.lock().unwrap().is_empty());

    let agent = fixture.agent(policy(PermissionMode::Default));
    for retry in [allow, HashMap::new()] {
        let (envelopes, error) =
            drain(agent.approve(run_id.clone(), retry, RunOptions::new("s1"))).await;
        assert!(envelopes.is_empty());
        assert!(
            matches!(error, Some(RunError::NotSuspended { .. })),
            "{error:?}"
        );
        // The message tells a host what to do next.
        let text = error.unwrap().to_string();
        assert!(text.contains("resume"), "{text}");
        let after = fixture.stores.runs.load(NS, &run_id).await.unwrap();
        assert_eq!(after.epoch, record.epoch, "refused before the claim");
        assert_eq!(after.last_seq, record.last_seq, "nothing written");
    }

    let envelopes = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert_eq!(subtype(&envelopes), ResultSubtype::Success);
    assert_eq!(fixture.results().await, ["v0.0"]);
    assert_eq!(fixture.runs.lock().unwrap()["call_0_0"], 1);
    assert_eq!(fixture.status(&run_id).await, RunStatus::Completed);
}
