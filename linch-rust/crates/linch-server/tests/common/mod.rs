//! Shared helpers: a server config for one tenant, a running listener
//! set, and a scripted run read from a client's notifications.

#![allow(dead_code)]

use std::path::Path;
use std::time::Duration;

use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::{AgentOpened, RunAccepted, method};
use linch_rpc::{Client, Notifications};
use linch_server::{Listeners, ServerConfig};
use serde_json::{Value, json};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

pub const TOKEN: &str = "tok-acme-p11c-transport-0001";

/// A config with tenant `acme` (token [`TOKEN`], scripted provider
/// allowed) and the listeners in `listeners`.
pub fn config(listeners: Value) -> ServerConfig {
    let mut config = json!({"tenants": {"acme": {"tokens": [{"value": TOKEN}],
                                                 "allow": {"scripted_provider": true}}}});
    for (key, value) in listeners.as_object().unwrap() {
        config[key] = value.clone();
    }
    ServerConfig::from_json(&config.to_string()).unwrap()
}

/// The listeners of `config`, serving until the returned token fires.
pub async fn running(config: &ServerConfig) -> (Running, Listening) {
    let tokens = config.tokens(|_| None).unwrap();
    let listeners = Listeners::bind(config, tokens).await.unwrap();
    let listening = Listening {
        socket: listeners.socket_path().map(Path::to_path_buf),
        http: listeners.http_addr(),
    };
    let stop = CancellationToken::new();
    let task = tokio::spawn(listeners.serve(stop.clone()));
    (Running { stop, task }, listening)
}

pub struct Listening {
    pub socket: Option<std::path::PathBuf>,
    pub http: Option<std::net::SocketAddr>,
}

pub struct Running {
    stop: CancellationToken,
    task: JoinHandle<std::io::Result<()>>,
}

impl Running {
    /// Stop the listeners and wait for every connection to end.
    pub async fn stop(self) {
        self.stop.cancel();
        let stopped = tokio::time::timeout(Duration::from_secs(10), self.task).await;
        stopped.expect("the listeners stop").unwrap().unwrap();
    }
}

/// A scripted agent config in namespace `acme` answering `text`.
pub fn scripted(text: &str) -> Value {
    let turn = TurnBuilder::new("scripted-model").text(text).build();
    json!({"namespace": "acme", "spec": {"model": "scripted-model"},
           "provider": {"id": "scripted", "turns": [turn]}})
}

/// Open a scripted agent, run it, and return its final text.
pub async fn run(client: &Client, notifications: &mut Notifications, text: &str) -> String {
    let opened: AgentOpened = client
        .call(method::AGENT_OPEN, &scripted(text))
        .await
        .unwrap();
    let params = json!({"agent": opened.agent, "session_id": "s1", "prompt": "hi"});
    let accepted: RunAccepted = client.call(method::RUN_START, &params).await.unwrap();
    let mut last = Value::Null;
    loop {
        let notification = notifications.recv().await.expect("connection open");
        assert_eq!(notification.params["run_id"], json!(accepted.run_id));
        match notification.method.as_str() {
            method::RUN_EVENT => last = notification.params["event"].clone(),
            method::RUN_END => break,
            other => panic!("unexpected {other}"),
        }
    }
    assert_eq!(last["type"], "result", "{last}");
    last["final_text"].as_str().unwrap().to_owned()
}
