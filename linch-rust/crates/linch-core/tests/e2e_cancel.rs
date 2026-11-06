//! A run cancel interrupts a hook the run is waiting on, at every point a
//! run waits on hooks, and approve or resume with a cancelled token aborts
//! before planning again (R1-rpc 4, R1-core 7).

mod common;

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::hooks::{ApprovalResponse, PreToolUse, PreToolUseRequest, PreToolUseResponse};
use linch_core::{
    Agent, AgentBuilder, CallbackError, Envelope, Event, Hook, Hooks, PermissionMode, Point,
    RunOptions, RunStatus, RunStore, ScriptedProvider, ScriptedTurn, ToolScope,
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

/// A hook that never answers; it says when it was called.
struct Waits(Arc<Notify>);

#[async_trait]
impl<P: Point> Hook<P> for Waits {
    fn policy_id(&self) -> &str {
        "waits"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(&self, _request: &P::Request) -> Result<P::Response, CallbackError> {
        self.0.notify_one();
        std::future::pending().await
    }
}

/// A `pre_tool_use` hook that counts its calls and lets every call through.
struct Counts(Arc<AtomicU32>);

#[async_trait]
impl Hook<PreToolUse> for Counts {
    fn policy_id(&self) -> &str {
        "counts"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        _request: &PreToolUseRequest,
    ) -> Result<PreToolUseResponse, CallbackError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(PreToolUseResponse::Continue)
    }
}

fn hooked(turns: Vec<ScriptedTurn>, hooks: Arc<Hooks>, stores: Arc<Stores>) -> Agent {
    let runs = Runs::default();
    AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new(turns)))
        .tools(registry(vec![Echo::new("Write", ToolScope::Write, &runs)]))
        .permissions(policy(PermissionMode::Default))
        .hooks(hooks)
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn subtype(envelopes: &[Envelope]) -> ResultSubtype {
    match &envelopes.last().unwrap().event {
        Event::Result(result) => result.subtype,
        other => panic!("expected a result, got {other:?}"),
    }
}

#[tokio::test]
async fn a_cancel_interrupts_the_hook_a_run_waits_on() {
    let points = [
        "pre_tool_use",
        "permission_request",
        "context_build",
        "stop",
        "post_turn",
    ];
    for point in points {
        let entered = Arc::new(Notify::new());
        let hooks = Hooks::default();
        let waits = || Arc::new(Waits(Arc::clone(&entered)));
        let _ = match point {
            "pre_tool_use" => hooks.pre_tool_use.register(waits()),
            "permission_request" => hooks.permission_request.register(waits()),
            "context_build" => hooks.context_build.register(waits()),
            "stop" => hooks.stop.register(waits()),
            _ => hooks.post_turn.register(waits()),
        }
        .unwrap();
        // The tool points need a call; the turn points a text answer.
        let turns = match point {
            "pre_tool_use" | "permission_request" => script(1, 1, "Write"),
            _ => vec![text_turn("done")],
        };
        let stores = Arc::new(Stores::default());
        let agent = hooked(turns, Arc::new(hooks), stores.clone());
        let mut options = RunOptions::new("s1");
        let token = CancellationToken::new();
        options.cancel = token.clone();
        let canceller = tokio::spawn(async move {
            entered.notified().await;
            token.cancel();
        });
        let stream = agent.run("go", options);
        let run_id = stream.run_id().clone();
        let ended = tokio::time::timeout(Duration::from_secs(5), finish(stream)).await;
        let envelopes =
            ended.unwrap_or_else(|_| panic!("{point}: the cancel did not end the wait"));
        canceller.await.unwrap();
        assert_eq!(subtype(&envelopes), ResultSubtype::Aborted, "{point}");
        let record = stores.runs.load(NS, &run_id).await.unwrap();
        assert_eq!(record.status, RunStatus::Aborted, "{point}");
    }
}

/// Approve with an answer for one of two calls, or resume, with a token
/// that is already cancelled, with or without a tool hook (R1-core 7,
/// R2-core 3): the stream ends empty, nothing is claimed or written, the
/// run stays suspended, and the hooks are not asked again.
#[tokio::test]
async fn approve_or_resume_with_a_cancelled_token_touches_nothing() {
    for (approving, counted) in [(true, true), (false, true), (true, false), (false, false)] {
        let calls = Arc::new(AtomicU32::new(0));
        let hooks = Hooks::default();
        if counted {
            let _ = (hooks.pre_tool_use)
                .register(Arc::new(Counts(Arc::clone(&calls))))
                .unwrap();
        }
        let hooks = Arc::new(hooks);
        let stores = Arc::new(Stores::default());
        let agent = hooked(script(1, 2, "Write"), hooks, stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        finish(stream).await;
        let planned = if counted { 2 } else { 0 };
        assert_eq!(calls.load(Ordering::SeqCst), planned, "both calls planned");
        let before = stores.runs.load(NS, &run_id).await.unwrap();
        let writes = stores.log.lock().unwrap().len();
        let options = RunOptions::new("s1");
        options.cancel.cancel();
        let stream = if approving {
            let answer = ("call_0_0".to_owned(), ApprovalResponse::Allow);
            agent.approve(run_id.clone(), HashMap::from([answer]), options)
        } else {
            agent.resume(run_id.clone(), options)
        };
        let envelopes = finish(stream).await;
        let case = format!("approve {approving}, hooked {counted}");
        assert!(envelopes.is_empty(), "{case}: {envelopes:?}");
        assert_eq!(calls.load(Ordering::SeqCst), planned, "no hook asked again");
        assert_eq!(stores.log.lock().unwrap().len(), writes, "{case}: no write");
        let record = stores.runs.load(NS, &run_id).await.unwrap();
        assert_eq!(record.status, RunStatus::AwaitingApproval, "{case}");
        assert_eq!(record.epoch, before.epoch, "{case}: no claim");
    }
}

/// A resume with a cancelled token of a run whose prompt is not in the
/// session yet writes nothing either: not the prompt, not an abort.
#[tokio::test]
async fn a_cancelled_resume_of_a_started_run_appends_no_prompt() {
    let stores = Arc::new(Stores::default());
    let agent = hooked(
        vec![text_turn("done")],
        Arc::new(Hooks::default()),
        stores.clone(),
    );
    // Drop the stream after its first envelope: created, prompt not appended.
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let first = futures::StreamExt::next(&mut stream)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(first.event, Event::Init(_)));
    drop(stream);
    let writes = stores.log.lock().unwrap().len();
    let options = RunOptions::new("s1");
    options.cancel.cancel();
    let envelopes = finish(agent.resume(run_id.clone(), options)).await;
    assert!(envelopes.is_empty());
    assert_eq!(stores.log.lock().unwrap().len(), writes, "no write");
    assert!(messages(&*stores, "s1").await.is_empty(), "no prompt");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Running);

    let envelopes = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert_eq!(subtype(&envelopes), ResultSubtype::Success);
}
