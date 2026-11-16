//! The observe-only points added for parity with Python's lifecycle hooks:
//! `agent_start`/`agent_stop` bracket the run, `turn_start` and
//! `provider_call_start`/`provider_call_stop` bracket each turn's dispatch,
//! and `post_tool_use_failure` fires after `post_tool_use` when the result
//! is an error. `tool_use_start`/`tool_use_stop` are covered in
//! `e2e_hooks.rs` (alongside `pre_tool_use`/`post_tool_use`, whose relative
//! order matters); `pre_compact`/`post_compact` in `e2e_compaction.rs`;
//! `subagent_start`/`subagent_stop` in `e2e_subagents.rs`; `event_emit`
//! below.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::events::model_call::CallDisposition;
use linch_core::hooks::{
    AgentStart, AgentStartRequest, AgentStop, AgentStopRequest, EventEmit, PostToolUseFailure,
    PostToolUseFailureRequest, ProviderCallStart, ProviderCallStartRequest, ProviderCallStop,
    ProviderCallStopRequest, TurnStart, TurnStartRequest,
};
use linch_core::{
    Agent, AgentBuilder, Hooks, JsonObject, PermissionMode, RunOptions, RunStatus, StopReason,
    Tool, ToolContext, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;

fn hooked(provider: Arc<Replay>, hooks: Hooks, stores: Arc<Stores>) -> Agent {
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

#[tokio::test]
async fn agent_start_fires_once_with_the_submitted_prompt_and_tools() {
    let starts: Arc<Mutex<Vec<AgentStartRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&starts);
    let hooks = Hooks::default();
    let on_start = hook::<AgentStart>("watch", move |request| {
        log.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.agent_start.register(on_start.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = hooked(provider, hooks, stores);
    let run_id = {
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        finish(stream).await;
        run_id
    };
    let starts = starts.lock().unwrap();
    assert_eq!(starts.len(), 1, "fires once, not once per turn");
    let start = &starts[0];
    assert_eq!(start.run_id, run_id);
    assert_eq!(start.model, MODEL);
    assert_eq!(start.prompt, "go");
    assert_eq!(start.tools, vec!["Read".to_owned()]);
}

/// A Rust adaptation from Python: a blocked prompt never creates a run, so
/// there is nothing to report either `agent_start` or `agent_stop` on.
#[tokio::test]
async fn agent_start_and_agent_stop_do_not_fire_when_the_prompt_is_blocked() {
    use linch_core::hooks::{UserPromptResponse, UserPromptSubmit};

    let starts: Arc<Mutex<u32>> = Arc::new(Mutex::new(0));
    let stops: Arc<Mutex<u32>> = Arc::new(Mutex::new(0));
    let hooks = Hooks::default();
    let moderate = hook::<UserPromptSubmit>("moderate", |_| {
        Ok(UserPromptResponse::Block {
            reason: "off topic".into(),
        })
    });
    let _ = hooks.user_prompt_submit.register(moderate.arc()).unwrap();
    let (log_start, log_stop) = (Arc::clone(&starts), Arc::clone(&stops));
    let on_start = hook::<AgentStart>("watch", move |_| {
        *log_start.lock().unwrap() += 1;
        Ok(())
    });
    let _ = hooks.agent_start.register(on_start.arc()).unwrap();
    let on_stop = hook::<AgentStop>("watch", move |_| {
        *log_stop.lock().unwrap() += 1;
        Ok(())
    });
    let _ = hooks.agent_stop.register(on_stop.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = hooked(provider, hooks, stores);
    let (envelopes, error) = drain(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        kinds(&envelopes),
        ["hook"],
        "only the prompt hook's own record"
    );
    assert!(
        error.is_some(),
        "a blocked prompt ends the stream with an error"
    );
    assert_eq!(*starts.lock().unwrap(), 0);
    assert_eq!(*stops.lock().unwrap(), 0);
}

#[tokio::test]
async fn agent_stop_fires_once_with_the_runs_terminal_status() {
    let stops: Arc<Mutex<Vec<AgentStopRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&stops);
    let hooks = Hooks::default();
    let on_stop = hook::<AgentStop>("watch", move |request| {
        log.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.agent_stop.register(on_stop.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = hooked(provider, hooks, stores);
    let run_id = {
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        finish(stream).await;
        run_id
    };
    let stops = stops.lock().unwrap();
    assert_eq!(stops.len(), 1, "end() is the single terminal writer");
    let stop = &stops[0];
    assert_eq!(stop.run_id, run_id);
    assert_eq!(stop.status, RunStatus::Completed);
    assert_eq!(stop.stop_reason, StopReason::EndTurn);
}

#[tokio::test]
async fn turn_start_fires_before_each_turns_dispatch_with_its_index() {
    let turns: Arc<Mutex<Vec<u32>>> = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&turns);
    let hooks = Hooks::default();
    let on_turn = hook::<TurnStart>("watch", move |request: &TurnStartRequest| {
        log.lock().unwrap().push(request.turn);
        Ok(())
    });
    let _ = hooks.turn_start.register(on_turn.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]));
    let agent = hooked(provider, hooks, stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(*turns.lock().unwrap(), [0, 1], "one turn_start per turn");
}

#[tokio::test]
async fn provider_call_start_and_stop_cover_every_dispatch() {
    let starts: Arc<Mutex<Vec<ProviderCallStartRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let stops: Arc<Mutex<Vec<ProviderCallStopRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let hooks = Hooks::default();
    let log_start = Arc::clone(&starts);
    let on_start = hook::<ProviderCallStart>("watch", move |request| {
        log_start.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.provider_call_start.register(on_start.arc()).unwrap();
    let log_stop = Arc::clone(&stops);
    let on_stop = hook::<ProviderCallStop>("watch", move |request| {
        log_stop.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.provider_call_stop.register(on_stop.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]));
    let agent = hooked(provider, hooks, stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let starts = starts.lock().unwrap();
    let stops = stops.lock().unwrap();
    assert_eq!(starts.len(), 2, "one call per turn");
    assert_eq!(stops.len(), 2);
    for (start, stop) in starts.iter().zip(stops.iter()) {
        assert_eq!(start.turn, stop.turn);
        assert_eq!(start.model, MODEL);
        assert_eq!(stop.model, MODEL);
        assert_eq!(stop.disposition, CallDisposition::Accepted);
        assert_eq!(stop.usage, Some(usage(10, 5)));
    }
    assert_eq!(starts.iter().map(|r| r.turn).collect::<Vec<_>>(), [0, 1]);
}

/// A tool that always fails, so `post_tool_use_failure` has something to
/// observe.
struct AlwaysFails {
    schema: JsonObject,
}

impl AlwaysFails {
    fn new() -> Self {
        Self {
            schema: json!({"type": "object"}).as_object().unwrap().clone(),
        }
    }
}

#[async_trait]
impl Tool for AlwaysFails {
    fn name(&self) -> &str {
        "Boom"
    }
    fn description(&self) -> &str {
        "Always fails."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Err(ToolOutputError::new("boom"))
    }
}

#[tokio::test]
async fn post_tool_use_failure_fires_only_when_the_result_is_an_error() {
    use linch_core::hooks::{PostToolUse, PostToolUseResponse};

    let failures: Arc<Mutex<Vec<PostToolUseFailureRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&failures);
    let hooks = Hooks::default();
    // post_tool_use runs first and leaves the error as it stands (open by
    // default), so post_tool_use_failure still sees it.
    let seen_by_post = hook::<PostToolUse>("scan", |_| Ok(PostToolUseResponse::Continue));
    let _ = hooks.post_tool_use.register(seen_by_post.arc()).unwrap();
    let on_failure = hook::<PostToolUseFailure>("watch", move |request| {
        log.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks
        .post_tool_use_failure
        .register(on_failure.arc())
        .unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 1, "Read"),
        tool_turn(1, 1, "Boom"),
        text_turn("done"),
    ]));
    let tools = ToolRegistry::new();
    let _ = tools
        .register(Arc::new(Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )))
        .unwrap();
    let _ = tools.register(Arc::new(AlwaysFails::new())).unwrap();
    let agent = AgentBuilder::new(spec())
        .provider(provider)
        .tools(Arc::new(tools))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let (envelopes, error) = drain(stream).await;
    assert!(
        error.is_none(),
        "a failed tool keeps the run going: {envelopes:?}"
    );

    let failures = failures.lock().unwrap();
    assert_eq!(failures.len(), 1, "only the failing call");
    let failure = &failures[0];
    assert_eq!(failure.run_id, run_id);
    assert_eq!(failure.tool_name, "Boom");
    assert_eq!(failure.error.message, "boom");
}

/// `event_emit` fires once per durable event, in delivery order, exactly
/// the caller's durable envelopes: never for a hook's own record (it would
/// otherwise recurse into itself) and never for a stream-only delta — both
/// still reach the caller, just never the `ready` batch `event_emit` runs
/// over.
#[tokio::test]
async fn event_emit_fires_for_every_durable_event_and_skips_stream_only_ones() {
    let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&seen);
    let hooks = Hooks::default();
    let watch = hook::<EventEmit>("mirror", move |request| {
        log.lock().unwrap().push(kind(&request.event));
        Ok(())
    });
    let _ = hooks.event_emit.register(watch.arc()).unwrap();
    // A second observe-only hook so the stream also carries a `hook` kind,
    // to show event_emit never sees it.
    let on_start = hook::<AgentStart>("audit", |_| Ok(()));
    let _ = hooks.agent_start.register(on_start.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = hooked(provider, hooks, stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let stream_only = ["hook", "partial_assistant"];
    for kind in stream_only {
        assert!(
            kinds(&envelopes).contains(&kind.to_owned()),
            "a {kind} envelope exists in the stream: {:?}",
            kinds(&envelopes)
        );
    }
    let durable: Vec<String> = (kinds(&envelopes).into_iter())
        .filter(|kind| !stream_only.contains(&kind.as_str()))
        .collect();
    assert_eq!(*seen.lock().unwrap(), durable);
}
