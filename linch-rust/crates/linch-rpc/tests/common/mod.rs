//! Shared helpers: an in-process client with typed calls, scripted agent
//! configs, and notifications filed per run.

#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use linch::provider::scripted::TurnBuilder;
use linch::{Envelope, Event, JsonObject, RunId, ScriptedTurn};
use linch_rpc::protocol::{AgentOpened, RunAccepted, RunEnd, method};
use linch_rpc::{Client, ClientError, ErrorObject, Notifications, RequestHandler};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

pub const MODEL: &str = "scripted-model";

/// What one run sent: its envelopes and the `run.end` error, if any.
#[derive(Debug)]
pub struct Ended {
    pub envelopes: Vec<Envelope>,
    pub error: Option<ErrorObject>,
}

impl Ended {
    pub fn kinds(&self) -> Vec<String> {
        self.envelopes.iter().map(|e| kind(&e.event)).collect()
    }

    pub fn last(&self) -> &Event {
        &self.envelopes.last().expect("no envelopes").event
    }
}

/// A client on an in-process server, with notifications filed per run.
pub struct Rpc {
    pub client: Client,
    notifications: Notifications,
    events: HashMap<RunId, VecDeque<Envelope>>,
    ends: HashMap<RunId, Option<ErrorObject>>,
}

impl Rpc {
    pub fn new() -> Self {
        Self::connected(Client::in_process())
    }

    /// A client whose server requests go to `handler`.
    pub fn with_handler(handler: Arc<dyn RequestHandler>) -> Self {
        Self::connected(Client::in_process_with(handler))
    }

    /// Wrap a connected client.
    pub fn connected((client, notifications): (Client, Notifications)) -> Self {
        Self {
            client,
            notifications,
            events: HashMap::new(),
            ends: HashMap::new(),
        }
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, ClientError> {
        self.client.call(method, &params).await
    }

    pub async fn open(&self, config: Value) -> String {
        let opened: AgentOpened = self.client.call(method::AGENT_OPEN, &config).await.unwrap();
        opened.agent
    }

    pub async fn accepted(&self, method: &str, params: Value) -> RunId {
        let accepted: RunAccepted = self.client.call(method, &params).await.unwrap();
        accepted.run_id
    }

    pub async fn start(&self, agent: &str, session: &str, prompt: &str) -> RunId {
        let params = json!({"agent": agent, "session_id": session, "prompt": prompt});
        self.accepted(method::RUN_START, params).await
    }

    /// The next envelope of `run_id`; `None` once its `run.end` arrived.
    pub async fn next_event(&mut self, run_id: &RunId) -> Option<Envelope> {
        loop {
            if let Some(envelope) = self.events.get_mut(run_id).and_then(VecDeque::pop_front) {
                return Some(envelope);
            }
            if self.ends.contains_key(run_id) {
                return None;
            }
            self.pump().await;
        }
    }

    /// Every envelope of `run_id` up to its `run.end`, and the end's error.
    pub async fn until_end(&mut self, run_id: &RunId) -> Ended {
        let mut envelopes = Vec::new();
        while let Some(envelope) = self.next_event(run_id).await {
            envelopes.push(envelope);
        }
        let error = self.ends.remove(run_id).expect("ended");
        Ended { envelopes, error }
    }

    /// File one notification under its run.
    async fn pump(&mut self) {
        let notification = self.notifications.recv().await.expect("server closed");
        match notification.method.as_str() {
            method::RUN_EVENT => {
                let envelope: Envelope = serde_json::from_value(notification.params).unwrap();
                let queue = self.events.entry(envelope.run_id.clone()).or_default();
                queue.push_back(envelope);
            }
            method::RUN_END => {
                let end: RunEnd = serde_json::from_value(notification.params).unwrap();
                self.ends.insert(end.run_id, end.error);
            }
            other => panic!("unexpected notification {other}"),
        }
    }
}

pub fn kind(event: &Event) -> String {
    event.to_value().unwrap()["type"]
        .as_str()
        .unwrap()
        .to_owned()
}

/// An agent config on the scripted provider; with a workspace, the
/// permission mode is `mode`.
pub fn scripted(turns: Vec<ScriptedTurn>, workspace: Option<&Path>, mode: &str) -> Value {
    let mut config = json!({
        "namespace": "rpc",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
    });
    if let Some(root) = workspace {
        config["workspace"] = json!(root);
        config["permissions"] = json!({"mode": mode, "project_root": root});
    }
    config
}

pub fn tool_turn(id: &str, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL).tool_call(id, tool, &input).build()
}

pub fn text_turn(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL).text(text).build()
}

/// A scripted agent config with no workspace, permission mode `mode`
/// (project root `/`), and the members of `extra` (`tools`, ...) added.
pub fn hosted(turns: Vec<ScriptedTurn>, mode: &str, extra: Value) -> Value {
    let mut config = scripted(turns, None, mode);
    config["permissions"] = json!({"mode": mode, "project_root": "/"});
    for (key, value) in extra.as_object().expect("extra is an object") {
        config[key] = value.clone();
    }
    config
}

/// A host tool declaration taking an optional string `q`.
pub fn host_tool(name: &str, scope: &str) -> Value {
    json!({
        "name": name,
        "description": format!("The {name} tool."),
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "scope": scope,
    })
}

/// The `result` block of each `tool_call_end`, as JSON, in order.
pub fn tool_results(ended: &Ended) -> Vec<Value> {
    (ended.envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) => Some(serde_json::to_value(&end.result).unwrap()),
            _ => None,
        })
        .collect()
}

/// A [`RequestHandler`] from an async closure over the method, the params
/// and the cancel token.
pub fn handler<F, Fut>(answer: F) -> Arc<dyn RequestHandler>
where
    F: Fn(String, JsonObject, CancellationToken) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<Value, ErrorObject>> + Send + 'static,
{
    Arc::new(Closure(answer))
}

struct Closure<F>(F);

#[async_trait]
impl<F, Fut> RequestHandler for Closure<F>
where
    F: Fn(String, JsonObject, CancellationToken) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<Value, ErrorObject>> + Send + 'static,
{
    async fn handle(
        &self,
        method: &str,
        params: JsonObject,
        cancel: CancellationToken,
    ) -> Result<Value, ErrorObject> {
        (self.0)(method.to_owned(), params, cancel).await
    }
}
