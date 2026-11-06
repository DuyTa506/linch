//! Shared helpers: an in-process client with typed calls, scripted agent
//! configs, and notifications filed per run.

#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};
use std::path::Path;

use linch::provider::scripted::TurnBuilder;
use linch::{Envelope, Event, RunId, ScriptedTurn};
use linch_rpc::protocol::{AgentOpened, RunAccepted, RunEnd, method};
use linch_rpc::{Client, ClientError, ErrorObject, Notifications};
use serde_json::{Value, json};

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
        let (client, notifications) = Client::in_process();
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
