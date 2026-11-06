//! Hook tests. The shared helpers and the ordering, rewrite, short-circuit
//! and #102 cases live here; failure handling and registration lifetime in
//! `tests/chain.rs`, per-point semantics and the digest in
//! `tests/points.rs`, property tests in `tests/props.rs`.

mod chain;
mod points;
mod props;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::json;

use super::*;
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::policy::FailMode;
use crate::registration::Registration;
use crate::tool::{ToolOutput, ToolResult};

/// Ids of the hooks called, in call order.
type Log = Arc<Mutex<Vec<String>>>;

/// What a [`Scripted`] hook does when called.
enum Act<R> {
    Answer(R),
    Fail,
    Hang,
}

type Behaviour<P> =
    Box<dyn Fn(&<P as Point>::Request) -> Act<<P as Point>::Response> + Send + Sync>;

/// A hook driven by a closure, logging its id on every call.
struct Scripted<P: Point> {
    id: String,
    version: u32,
    matcher: Option<ToolMatcher>,
    timeout: Option<Duration>,
    fail_mode: Option<FailMode>,
    log: Log,
    act: Behaviour<P>,
}

impl<P: Point> Scripted<P> {
    fn new(
        id: &str,
        log: &Log,
        act: impl Fn(&P::Request) -> Act<P::Response> + Send + Sync + 'static,
    ) -> Self {
        Self {
            id: id.to_owned(),
            version: 1,
            matcher: None,
            timeout: None,
            fail_mode: None,
            log: Arc::clone(log),
            act: Box::new(act),
        }
    }

    fn matcher(mut self, patterns: &[&str]) -> Self {
        self.matcher = Some(ToolMatcher::new(patterns.iter().copied()).unwrap());
        self
    }

    fn timeout(mut self, limit: Duration) -> Self {
        self.timeout = Some(limit);
        self
    }

    fn fail_mode(mut self, mode: FailMode) -> Self {
        self.fail_mode = Some(mode);
        self
    }

    fn version(mut self, version: u32) -> Self {
        self.version = version;
        self
    }
}

#[async_trait]
impl<P: Point> Hook<P> for Scripted<P> {
    fn policy_id(&self) -> &str {
        &self.id
    }
    fn policy_version(&self) -> u32 {
        self.version
    }
    fn matcher(&self) -> Option<ToolMatcher> {
        self.matcher.clone()
    }
    fn timeout(&self) -> Option<Duration> {
        self.timeout
    }
    fn fail_mode(&self) -> Option<FailMode> {
        self.fail_mode
    }
    async fn call(&self, request: &P::Request) -> Result<P::Response, CallbackError> {
        self.log.lock().unwrap().push(self.id.clone());
        match (self.act)(request) {
            Act::Answer(response) => Ok(response),
            Act::Fail => Err("backend down".into()),
            Act::Hang => std::future::pending().await,
        }
    }
}

fn new_log() -> Log {
    Arc::new(Mutex::new(Vec::new()))
}

fn calls(log: &Log) -> Vec<String> {
    log.lock().unwrap().clone()
}

fn obj(value: serde_json::Value) -> JsonObject {
    match value {
        serde_json::Value::Object(map) => map,
        other => panic!("not an object: {other}"),
    }
}

fn pre_request(tool: &str, input: JsonObject) -> PreToolUseRequest {
    PreToolUseRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: tool.into(),
        input,
    }
}

fn register<P: Point>(chain: &HookChain<P>, hook: Scripted<P>) -> Registration {
    chain.register(Arc::new(hook)).unwrap()
}

const SHORT: Duration = Duration::from_millis(5);

#[tokio::test]
async fn hooks_run_in_registration_order() {
    let hooks = Hooks::default();
    let log = new_log();
    for id in ["a", "b", "c"] {
        let _ = register(
            &hooks.pre_tool_use,
            Scripted::new(id, &log, |_| Act::Answer(PreToolUseResponse::Continue)),
        );
    }
    let done = hooks
        .pre_tool_use
        .run(pre_request("Write", obj(json!({}))))
        .await;
    assert_eq!(calls(&log), ["a", "b", "c"]);
    assert_eq!(done.records.len(), 3);
    assert!(matches!(done.outcome, PreToolUseOutcome::Run { .. }));
}

#[tokio::test]
async fn a_rewrite_feeds_the_next_hook() {
    let hooks = Hooks::default();
    let log = new_log();
    let seen = Arc::new(Mutex::new(None));
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("rewrite", &log, |_| {
            Act::Answer(PreToolUseResponse::Rewrite {
                input: obj(json!({"path": "/safe"})),
            })
        }),
    );
    let sink = Arc::clone(&seen);
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("observe", &log, move |req: &PreToolUseRequest| {
            *sink.lock().unwrap() = Some(req.input.clone());
            Act::Answer(PreToolUseResponse::Continue)
        }),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Write", obj(json!({"path": "/etc"}))))
        .await;
    let rewritten = obj(json!({"path": "/safe"}));
    assert_eq!(seen.lock().unwrap().as_ref(), Some(&rewritten));
    assert_eq!(done.outcome, PreToolUseOutcome::Run { input: rewritten });
}

#[tokio::test]
async fn a_block_stops_the_chain_and_keeps_the_final_input() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("rewrite", &log, |_| {
            Act::Answer(PreToolUseResponse::Rewrite {
                input: obj(json!({"n": 2})),
            })
        }),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("deny", &log, |_| {
            Act::Answer(PreToolUseResponse::Block {
                reason: "read first".into(),
            })
        }),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("never", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Write", obj(json!({"n": 1}))))
        .await;
    assert_eq!(calls(&log), ["rewrite", "deny"]);
    assert_eq!(
        done.outcome,
        PreToolUseOutcome::Blocked {
            input: obj(json!({"n": 2})),
            reason: "read first".into(),
        }
    );
}

#[tokio::test]
async fn a_resolve_stops_the_chain_without_running_the_tool() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("cache", &log, |_| {
            Act::Answer(PreToolUseResponse::Resolve {
                result: Ok(ToolOutput::new("cached")),
            })
        }),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("never", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Read", obj(json!({}))))
        .await;
    assert_eq!(calls(&log), ["cache"]);
    assert_eq!(
        done.outcome,
        PreToolUseOutcome::Resolved {
            input: obj(json!({})),
            result: Ok(ToolOutput::new("cached")),
        }
    );
}

/// Linch 3.x #102: a `retry`/`force_continue` verdict cut the PreToolUse
/// chain (skipping ReadBeforeWrite and ToolCache) and the tool still ran.
/// Here every step that stops the chain also stops the tool, and a fail-open
/// error skips only the failing hook, never the ones after it.
#[tokio::test]
async fn issue_102_no_step_skips_later_hooks_and_still_runs_the_tool() {
    type Stopper = fn(&str, &Log) -> Scripted<PreToolUse>;
    let stoppers: [(&str, Stopper); 4] = [
        ("block", |id, log| {
            Scripted::new(id, log, |_| {
                Act::Answer(PreToolUseResponse::Block {
                    reason: "stale read".into(),
                })
            })
        }),
        ("resolve", |id, log| {
            Scripted::new(id, log, |_| {
                Act::Answer(PreToolUseResponse::Resolve {
                    result: Ok(ToolOutput::new("cached")),
                })
            })
        }),
        ("closed_error", |id, log| {
            Scripted::new(id, log, |_| Act::Fail).fail_mode(FailMode::Closed)
        }),
        ("closed_timeout", |id, log| {
            Scripted::new(id, log, |_| Act::Hang).timeout(SHORT)
        }),
    ];
    for (name, stopper) in stoppers {
        let hooks = Hooks::default();
        let log = new_log();
        let _ = register(
            &hooks.pre_tool_use,
            Scripted::new("flaky", &log, |_| Act::Fail).fail_mode(FailMode::Open),
        );
        let _ = register(
            &hooks.pre_tool_use,
            Scripted::new("read_before_write", &log, |_| {
                Act::Answer(PreToolUseResponse::Continue)
            }),
        );
        let _ = register(&hooks.pre_tool_use, stopper(name, &log));
        let _ = register(
            &hooks.pre_tool_use,
            Scripted::new("after", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
        );
        let done = hooks
            .pre_tool_use
            .run(pre_request("Write", obj(json!({}))))
            .await;
        assert_eq!(calls(&log), ["flaky", "read_before_write", name]);
        assert!(
            !matches!(done.outcome, PreToolUseOutcome::Run { .. }),
            "{name} cut the chain, so the tool must not run"
        );
    }
}
