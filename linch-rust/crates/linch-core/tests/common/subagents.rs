//! Fixtures for the subagent tests: a provider that answers a parent and
//! its children by the request, a `Subagent` call, and a store that dies.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Commit, Envelope, Lease, LogEntry, NewRun, Provider, ProviderError, ProviderRequest,
    ProviderStream, Role, RunId, RunRecord, RunStore, ScriptedTurn, SessionId, SessionStore,
    StoreError,
};
use serde_json::json;
use tokio::sync::Barrier;
use tokio_util::sync::CancellationToken;

use super::{MODEL, usage};

/// Who a request is for: `parent`, or a child's prompt.
pub fn who(request: &ProviderRequest) -> String {
    let child = (request.system.iter()).any(|b| b.text.starts_with("User-provided instructions"));
    match child {
        true => request.messages[0].text(),
        false => "parent".into(),
    }
}

/// Answers the parent with `parent[n]` and every child with `child[n]`,
/// `n` being the assistant messages in the request. Counts calls per
/// (who, n) and keeps every request.
pub struct Family {
    pub parent: Vec<ScriptedTurn>,
    pub child: Vec<ScriptedTurn>,
    pub calls: Mutex<HashMap<(String, usize), u32>>,
    pub requests: Mutex<Vec<ProviderRequest>>,
    /// Children's first calls wait here until this many arrive.
    pub meet: Option<Barrier>,
}

impl Family {
    pub fn new(parent: Vec<ScriptedTurn>, child: Vec<ScriptedTurn>) -> Self {
        Self {
            parent,
            child,
            calls: Mutex::default(),
            requests: Mutex::default(),
            meet: None,
        }
    }

    /// Every request for `who`, in call order.
    pub fn requests_of(&self, who_: &str) -> Vec<ProviderRequest> {
        let requests = self.requests.lock().unwrap();
        requests
            .iter()
            .filter(|r| who(r) == who_)
            .cloned()
            .collect()
    }
}

#[async_trait]
impl Provider for Family {
    fn id(&self) -> &str {
        "family"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.requests.lock().unwrap().push(request.clone());
        let n = (request.messages.iter())
            .filter(|m| m.role == Role::Assistant)
            .count();
        let who = who(&request);
        *self
            .calls
            .lock()
            .unwrap()
            .entry((who.clone(), n))
            .or_default() += 1;
        if let (Some(meet), true) = (&self.meet, who != "parent" && n == 0) {
            let met = tokio::time::timeout(Duration::from_secs(5), meet.wait()).await;
            if met.is_err() {
                let message = "the children never met".into();
                return Err(ProviderError::Transport { message });
            }
        }
        let turns = if who == "parent" {
            &self.parent
        } else {
            &self.child
        };
        let events: Vec<_> = turns[n].events.iter().cloned().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

/// A response with one `Subagent` call per prompt, ids `sub_{i}`.
pub fn spawn_turn(subagent_type: Option<&str>, prompts: &[&str]) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL);
    for (i, prompt) in prompts.iter().enumerate() {
        let mut input = json!({"description": "look around", "prompt": prompt});
        if let Some(kind) = subagent_type {
            input["subagent_type"] = json!(kind);
        }
        builder = builder.tool_call(&format!("sub_{i}"), "Subagent", &input);
    }
    builder.usage(usage(10, 5)).build()
}

/// A store whose write `n` fails and every write after it too, like a
/// process that died there. Reads still work.
pub struct Dying<S> {
    faulty: Faulty<S>,
    dead: Mutex<bool>,
}

impl<S> Dying<S> {
    pub fn new(inner: Arc<S>, n: u64, timing: FaultTiming) -> Self {
        Self {
            faulty: Faulty::new(inner, n, timing),
            dead: Mutex::new(false),
        }
    }

    fn check<T>(&self, result: Result<T, StoreError>) -> Result<T, StoreError> {
        let mut dead = self.dead.lock().unwrap();
        if *dead {
            return Err(StoreError::Conflict {
                message: "dead".into(),
            });
        }
        *dead = matches!(result, Err(StoreError::Backend(_)));
        result
    }

    fn alive(&self) -> Result<(), StoreError> {
        self.check(Ok(()))
    }
}

#[async_trait]
impl<S: RunStore + SessionStore + 'static> RunStore for Dying<S> {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.alive()?;
        self.check(RunStore::create(&self.faulty, run).await)
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.alive()?;
        self.check(self.faulty.claim(ns, run_id, owner).await)
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&self.faulty, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.alive()?;
        self.check(self.faulty.commit(lease, commit).await)
    }
    async fn events(&self, ns: &str, id: &RunId, after: u64) -> Result<Vec<Envelope>, StoreError> {
        self.faulty.events(ns, id, after).await
    }
}

#[async_trait]
impl<S: RunStore + SessionStore + 'static> SessionStore for Dying<S> {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        self.alive()?;
        self.check(SessionStore::create(&self.faulty, ns, session).await)
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&self.faulty, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.alive()?;
        self.check(self.faulty.append(ns, session, expected, entries).await)
    }
}
