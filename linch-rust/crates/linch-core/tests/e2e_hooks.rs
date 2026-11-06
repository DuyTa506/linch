//! The prompt, context and tool hook points in a run: a rewrite or block
//! before the run exists, request-only context, and stream-only records.

mod common;

use std::sync::Arc;

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{HookEvent, HookOutcome};
use linch_core::hooks::{
    ContextBuild, PostToolUse, PostToolUseResponse, PreToolUse, PreToolUseResponse,
    UserPromptResponse, UserPromptSubmit,
};
use linch_core::{
    Agent, AgentBuilder, ContentBlock, Envelope, Event, FailMode, HookPoint, Hooks, Message,
    PermissionMode, Role, RunError, RunOptions, RunStore, ScriptedProvider, SessionId,
    SessionStore, StoreError, TextBlock, ToolScope,
};

fn hooked(provider: Arc<ScriptedProvider>, hooks: Hooks, stores: Arc<Stores>) -> Agent {
    let runs = Runs::default();
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn hook_events(envelopes: &[Envelope]) -> Vec<&HookEvent> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Hook(record) => Some(record),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn a_rewritten_prompt_starts_the_run() {
    let hooks = Hooks::default();
    let rewrite = hook::<UserPromptSubmit>("redact", |_| {
        let content = vec![ContentBlock::text("rewritten")];
        Ok(UserPromptResponse::Rewrite { content })
    });
    let _ = hooks.user_prompt_submit.register(rewrite.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new([text_turn("ok")]));
    let stores = Arc::new(Stores::default());
    let agent = hooked(provider.clone(), hooks, stores.clone());
    let envelopes = finish(agent.run("secret", RunOptions::new("s1"))).await;
    let kinds = kinds(&envelopes);
    assert_eq!(kinds[..3], ["hook", "init", "user"]);
    assert_eq!(
        envelopes[0].seq, 0,
        "the record goes out before anything is durable"
    );
    let Event::User(prompt) = &envelopes[2].event else {
        panic!("no prompt event");
    };
    assert_eq!(prompt.message, Message::user_text("rewritten"));
    assert_eq!(
        messages(&*stores, "s1").await[0],
        Message::user_text("rewritten")
    );
    assert_eq!(
        provider.requests()[0].messages[0],
        Message::user_text("rewritten")
    );
}

async fn blocked(fail_mode: Option<FailMode>) -> (Vec<Envelope>, RunError, Arc<Stores>) {
    let hooks = Hooks::default();
    let mut gate = hook::<UserPromptSubmit>("gate", move |_| match fail_mode {
        None => Ok(UserPromptResponse::Block {
            reason: "off topic".into(),
        }),
        Some(_) => Err("classifier down".into()),
    });
    if let Some(mode) = fail_mode {
        gate = gate.fail_mode(mode);
    }
    let _ = hooks.user_prompt_submit.register(gate.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = hooked(Arc::new(ScriptedProvider::new([])), hooks, stores.clone());
    let stream = agent.run("hi", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let (envelopes, error) = drain(stream).await;
    assert!(stores.log.lock().unwrap().is_empty(), "nothing is written");
    let missing = stores.runs.load(NS, &run_id).await;
    assert!(matches!(missing, Err(StoreError::NotFound { .. })));
    let session = SessionStore::load(&*stores, NS, &SessionId::new("s1")).await;
    assert!(matches!(session, Err(StoreError::NotFound { .. })));
    (
        envelopes,
        error.expect("the stream ends with an error"),
        stores,
    )
}

#[tokio::test]
async fn a_blocked_prompt_creates_no_run() {
    let (envelopes, error, _) = blocked(None).await;
    assert_eq!(kinds(&envelopes), ["hook"]);
    let verdict = &hook_events(&envelopes)[0].outcome;
    assert_eq!(
        *verdict,
        HookOutcome::Answered {
            verdict: "block".into()
        }
    );
    assert!(matches!(error, RunError::PromptBlocked { reason, .. } if reason == "off topic"));
}

#[tokio::test]
async fn a_failing_prompt_hook_blocks_only_when_it_fails_closed() {
    let (envelopes, error, _) = blocked(Some(FailMode::Closed)).await;
    let failed = HookOutcome::Failed {
        error: "classifier down".into(),
        fail_mode: FailMode::Closed,
    };
    assert_eq!(hook_events(&envelopes)[0].outcome, failed);
    assert!(
        matches!(error, RunError::PromptBlocked { reason, .. } if reason == "hook `gate` failed")
    );

    let hooks = Hooks::default();
    let down = hook::<UserPromptSubmit>("gate", |_| Err("classifier down".into()));
    let _ = hooks.user_prompt_submit.register(down.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([text_turn("ok")]));
    let agent = hooked(provider, hooks, stores.clone());
    let envelopes = finish(agent.run("hi", RunOptions::new("s1"))).await;
    let open = HookOutcome::Failed {
        error: "classifier down".into(),
        fail_mode: FailMode::Open,
    };
    assert_eq!(hook_events(&envelopes)[0].outcome, open, "open by default");
    assert_eq!(messages(&*stores, "s1").await[0], Message::user_text("hi"));
}

/// The request's last message, as text blocks.
fn tail_texts(message: &Message) -> Vec<String> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::Text(text) => Some(text.text.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn context_blocks_reach_the_request_and_not_the_log() {
    let hooks = Hooks::default();
    let context = hook::<ContextBuild>("rag", |request| {
        let n = request.messages.len();
        let blocks = vec![TextBlock {
            text: format!("CTX-{n}"),
        }];
        Ok(linch_core::hooks::ContextBuildResponse { blocks })
    });
    let _ = hooks.context_build.register(context.arc()).unwrap();
    let failing = hook::<ContextBuild>("down", |_| Err("index offline".into()));
    let _ = hooks.context_build.register(failing.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = hooked(provider.clone(), hooks, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let requests = provider.requests();
    let first = &requests[0].messages;
    assert_eq!(first.len(), 1);
    assert_eq!(tail_texts(&first[0]), ["go", "CTX-1"]);
    let second = &requests[1].messages;
    assert_eq!(
        second[0],
        Message::user_text("go"),
        "earlier context is gone"
    );
    let last = second.last().unwrap();
    assert_eq!(last.role, Role::User);
    assert!(matches!(last.content[0], ContentBlock::ToolResult(_)));
    assert_eq!(tail_texts(last), ["CTX-3"]);
    assert_eq!(
        requests[1].system, requests[0].system,
        "the system prompt is untouched"
    );

    let log = serde_json::to_string(&messages(&*stores, "s1").await).unwrap();
    assert!(!log.contains("CTX-"), "the session never holds context");
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let events = serde_json::to_string(&events).unwrap();
    assert!(!events.contains("CTX-"), "the journal never holds context");
    let points: Vec<_> = hook_events(&envelopes).iter().map(|r| r.point).collect();
    assert_eq!(points, [HookPoint::ContextBuild; 4], "two hooks per turn");
    assert!(matches!(
        hook_events(&envelopes)[1].outcome,
        HookOutcome::Failed {
            fail_mode: FailMode::Open,
            ..
        }
    ));
}

#[tokio::test]
async fn tool_hook_records_are_streamed_and_never_stored() {
    let hooks = Hooks::default();
    let pre = hook::<PreToolUse>("audit", |_| Ok(PreToolUseResponse::Continue));
    let _ = hooks.pre_tool_use.register(pre.arc()).unwrap();
    let post = hook::<PostToolUse>("scan", |_| Ok(PostToolUseResponse::Continue));
    let _ = hooks.post_tool_use.register(post.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = hooked(provider, hooks, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let records = hook_events(&envelopes);
    let seen: Vec<_> = (records.iter())
        .map(|r| (r.point, r.tool_use_id.as_deref()))
        .collect();
    let call = Some("call_0_0");
    assert_eq!(
        seen,
        [
            (HookPoint::PreToolUse, call),
            (HookPoint::PostToolUse, call)
        ]
    );
    let stored = stores.runs.events(NS, &run_id, 0).await.unwrap();
    assert!(!kinds(&stored).contains(&"hook".to_owned()));
    let replay = agent.events(&run_id, 0).await.unwrap();
    assert!(!kinds(&replay).contains(&"hook".to_owned()));
    let commits = stores.runs.writes().commit;
    assert_eq!(
        commits,
        5 + 3,
        "a tool turn and a text turn: hooks add no write"
    );
}
