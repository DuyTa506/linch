//! The `tool_execute` waterfall: middleware around the tool body.
//!
//! Python's `tools/execute` point. A [`ToolMiddleware`] wraps one attempt of
//! one tool call: it receives the call and a [`Next`], and runs the inner
//! layers and the tool by awaiting [`Next::run`]. Not calling it vetoes the
//! call: the layers inside and the tool never run, and what the middleware
//! returns is the result. The first registered layer is the outermost.
//!
//! `Next` is consumed by `run`, so a layer can run what it wraps at most
//! once. The call a layer sees is a shared reference: the input is the one
//! permission already checked, and a layer cannot change it. Input
//! rewrites belong in `pre_tool_use`, where the input is validated and
//! checked again.
//!
//! With no layer registered the scheduler calls the tool directly, as it
//! did before this seam existed.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;
use futures::future::BoxFuture;

use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::policy::HookPoint;
use crate::registration::Registration;
use crate::tool::ToolResult;

use super::hook::{HookError, ToolMatcher};

/// One attempt of a tool call that is about to run.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Tool being called.
    pub tool_name: String,
    /// The input permission allowed, which the tool runs with.
    pub input: JsonObject,
    /// Which attempt this is, from 1. Above 1 only when the agent's
    /// `tool_retry` policy runs the call again.
    pub attempt: u32,
}

/// What a [`ToolMiddleware`] runs by awaiting [`Next::run`]: the layers
/// inside it, then the tool.
#[must_use = "a middleware that drops Next vetoes the call"]
pub struct Next<'a> {
    run: Box<dyn FnOnce() -> BoxFuture<'a, ToolResult> + Send + 'a>,
}

impl<'a> Next<'a> {
    fn new(run: impl FnOnce() -> BoxFuture<'a, ToolResult> + Send + 'a) -> Self {
        Self { run: Box::new(run) }
    }

    /// Run the inner layers and the tool, and return their result.
    pub async fn run(self) -> ToolResult {
        (self.run)().await
    }
}

/// A layer around a tool's body.
///
/// The pipeline reads [`Self::policy_id`], [`Self::policy_version`] and
/// [`Self::matcher`] once, at registration. The policy enters the run
/// contract, so bump the version whenever the layer's behaviour changes.
#[async_trait]
pub trait ToolMiddleware: Send + Sync {
    /// Stable id of the policy this layer enforces.
    fn policy_id(&self) -> &str;

    /// Version of the policy.
    fn policy_version(&self) -> u32;

    /// Tool names this layer wraps. `None` wraps every tool; a call the
    /// matcher refuses skips the layer.
    fn matcher(&self) -> Option<ToolMatcher> {
        None
    }

    /// Wrap one attempt: await `next.run()` to run what is inside, or
    /// return without it to veto the call.
    async fn call(&self, call: &ToolCall, next: Next<'_>) -> ToolResult;
}

/// The middleware registered for one agent, outermost first.
///
/// Shared across runs. Each call works on the layers registered when it
/// starts.
pub struct ToolPipeline {
    state: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    next_id: u64,
    layers: Arc<[Layer]>,
}

#[derive(Clone)]
struct Layer {
    id: u64,
    middleware: Arc<dyn ToolMiddleware>,
    policy_id: Arc<str>,
    policy_version: u32,
    matcher: Option<ToolMatcher>,
}

impl Default for ToolPipeline {
    fn default() -> Self {
        Self {
            state: Arc::new(Mutex::new(State {
                next_id: 0,
                layers: Arc::from(Vec::new()),
            })),
        }
    }
}

impl ToolPipeline {
    /// Add `middleware` inside the layers already registered.
    ///
    /// Returns a [`Registration`] whose dispose removes this layer only;
    /// registering the same middleware twice gives two layers.
    ///
    /// # Errors
    ///
    /// [`HookError::BlankPolicyId`] for a blank policy id.
    pub fn register(&self, middleware: Arc<dyn ToolMiddleware>) -> Result<Registration, HookError> {
        let policy_id = middleware.policy_id();
        if policy_id.trim().is_empty() {
            return Err(HookError::BlankPolicyId);
        }
        let mut layer = Layer {
            id: 0,
            policy_id: Arc::from(policy_id),
            policy_version: middleware.policy_version(),
            matcher: middleware.matcher(),
            middleware,
        };
        let mut state = lock(&self.state);
        layer.id = state.next_id;
        state.next_id += 1;
        let id = layer.id;
        let mut layers = state.layers.to_vec();
        layers.push(layer);
        state.layers = layers.into();
        drop(state);

        let weak = Arc::downgrade(&self.state);
        Ok(Registration::new(move || {
            if let Some(state) = weak.upgrade() {
                let mut state = lock(&state);
                if state.layers.iter().any(|layer| layer.id == id) {
                    state.layers = state
                        .layers
                        .iter()
                        .filter(|layer| layer.id != id)
                        .cloned()
                        .collect();
                }
            }
        }))
    }

    /// Whether no layer is registered, so the scheduler calls the tool
    /// directly.
    pub fn is_empty(&self) -> bool {
        lock(&self.state).layers.is_empty()
    }

    /// `(point, policy_id, version)` of every layer, outermost first.
    pub(super) fn policies(&self) -> Vec<(HookPoint, Arc<str>, u32)> {
        let layers = Arc::clone(&lock(&self.state).layers);
        layers
            .iter()
            .map(|layer| {
                let id = Arc::clone(&layer.policy_id);
                (HookPoint::ToolExecute, id, layer.policy_version)
            })
            .collect()
    }

    /// Run `call` through the layers whose matcher accepts it, with
    /// `tool` as the innermost body.
    pub(crate) async fn run<'a>(
        &self,
        call: &'a ToolCall,
        tool: BoxFuture<'a, ToolResult>,
    ) -> ToolResult {
        let layers = Arc::clone(&lock(&self.state).layers);
        let mut next = Next::new(move || tool);
        for layer in layers.iter().rev() {
            if let Some(matcher) = &layer.matcher
                && !matcher.matches(&call.tool_name)
            {
                continue;
            }
            let middleware = Arc::clone(&layer.middleware);
            let inner = next;
            next = Next::new(move || Box::pin(async move { middleware.call(call, inner).await }));
        }
        next.run().await
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;
    use crate::tool::{ToolOutput, ToolOutputError};

    struct Layer1 {
        id: &'static str,
        log: Arc<Mutex<Vec<String>>>,
        veto: bool,
        matcher: Option<ToolMatcher>,
    }

    #[async_trait]
    impl ToolMiddleware for Layer1 {
        fn policy_id(&self) -> &str {
            self.id
        }
        fn policy_version(&self) -> u32 {
            1
        }
        fn matcher(&self) -> Option<ToolMatcher> {
            self.matcher.clone()
        }
        async fn call(&self, _call: &ToolCall, next: Next<'_>) -> ToolResult {
            lock(&self.log).push(format!("{} in", self.id));
            if self.veto {
                return Err(ToolOutputError::new(format!("{} vetoed", self.id)));
            }
            let result = next.run().await;
            lock(&self.log).push(format!("{} out", self.id));
            result
        }
    }

    fn layer(id: &'static str, log: &Arc<Mutex<Vec<String>>>, veto: bool) -> Arc<Layer1> {
        Arc::new(Layer1 {
            id,
            log: Arc::clone(log),
            veto,
            matcher: None,
        })
    }

    fn call(tool: &str) -> ToolCall {
        ToolCall {
            session_id: SessionId::from("s"),
            run_id: RunId::from("r"),
            tool_use_id: "t".into(),
            tool_name: tool.into(),
            input: JsonObject::new(),
            attempt: 1,
        }
    }

    fn body(
        log: &Arc<Mutex<Vec<String>>>,
        runs: &Arc<AtomicUsize>,
    ) -> BoxFuture<'static, ToolResult> {
        let (log, runs) = (Arc::clone(log), Arc::clone(runs));
        Box::pin(async move {
            runs.fetch_add(1, Ordering::SeqCst);
            lock(&log).push("tool".into());
            Ok(ToolOutput::new("ran"))
        })
    }

    #[tokio::test]
    async fn the_first_registered_layer_is_outermost() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let runs = Arc::new(AtomicUsize::new(0));
        let pipeline = ToolPipeline::default();
        let _a = pipeline.register(layer("a", &log, false)).unwrap();
        let _b = pipeline.register(layer("b", &log, false)).unwrap();
        let call = call("Read");
        let result = pipeline.run(&call, body(&log, &runs)).await;
        assert_eq!(result, Ok(ToolOutput::new("ran")));
        assert_eq!(
            *lock(&log),
            ["a in", "b in", "tool", "b out", "a out"].map(String::from)
        );
    }

    #[tokio::test]
    async fn a_layer_that_does_not_call_next_vetoes_the_rest() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let runs = Arc::new(AtomicUsize::new(0));
        let pipeline = ToolPipeline::default();
        let _a = pipeline.register(layer("a", &log, false)).unwrap();
        let _b = pipeline.register(layer("b", &log, true)).unwrap();
        let _c = pipeline.register(layer("c", &log, false)).unwrap();
        let call = call("Read");
        let result = pipeline.run(&call, body(&log, &runs)).await;
        assert_eq!(result, Err(ToolOutputError::new("b vetoed")));
        assert_eq!(runs.load(Ordering::SeqCst), 0);
        assert_eq!(*lock(&log), ["a in", "b in", "a out"].map(String::from));
    }

    #[tokio::test]
    async fn a_matcher_skips_a_layer_and_dispose_removes_it() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let runs = Arc::new(AtomicUsize::new(0));
        let pipeline = ToolPipeline::default();
        assert!(pipeline.is_empty());
        let only_write = Arc::new(Layer1 {
            id: "w",
            log: Arc::clone(&log),
            veto: true,
            matcher: Some(ToolMatcher::new(["Write"]).unwrap()),
        });
        let registration = pipeline.register(only_write).unwrap();
        assert!(!pipeline.is_empty());
        let read = call("Read");
        assert!(pipeline.run(&read, body(&log, &runs)).await.is_ok());
        let write = call("Write");
        assert!(pipeline.run(&write, body(&log, &runs)).await.is_err());
        registration.dispose();
        registration.dispose();
        assert!(pipeline.is_empty());
        assert!(pipeline.run(&write, body(&log, &runs)).await.is_ok());
    }

    #[test]
    fn a_blank_policy_id_is_refused_and_policies_keep_order() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let pipeline = ToolPipeline::default();
        assert_eq!(
            pipeline.register(layer(" ", &log, false)).err(),
            Some(HookError::BlankPolicyId)
        );
        let _a = pipeline.register(layer("a", &log, false)).unwrap();
        let _b = pipeline.register(layer("b", &log, false)).unwrap();
        let ids: Vec<_> = pipeline
            .policies()
            .into_iter()
            .map(|(_, id, _)| id.to_string())
            .collect();
        assert_eq!(ids, ["a", "b"]);
    }

    #[test]
    fn a_layer_is_part_of_the_policy_digest_and_none_changes_nothing() {
        use crate::hooks::Hooks;
        let log = Arc::new(Mutex::new(Vec::new()));
        let bare = Hooks::default().policy_digest();
        let hooks = Hooks::default();
        assert_eq!(hooks.policy_digest(), bare);
        let a = hooks
            .tool_execute
            .register(layer("a", &log, false))
            .unwrap();
        let one = hooks.policy_digest();
        assert_ne!(one, bare);
        let _b = hooks
            .tool_execute
            .register(layer("b", &log, false))
            .unwrap();
        assert_ne!(hooks.policy_digest(), one);
        a.dispose();
        let swapped = Hooks::default();
        let _ = swapped
            .tool_execute
            .register(layer("b", &log, false))
            .unwrap();
        assert_eq!(hooks.policy_digest(), swapped.policy_digest());
    }
}
