//! Spawns the `linch-server` binary and speaks the protocol over its stdio:
//! a scripted run end to end, then a DeepSeek run against a closed local
//! port whose key, set in the server's environment, must never come back.
//! Closing stdin stops the server; the test waits for it to exit.

use std::process::Stdio;
use std::time::Duration;

use linch::provider::scripted::TurnBuilder;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::time::timeout;

const SECRET: &str = "sk-linch-p11-test-secret-4242";

struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: Lines<BufReader<ChildStdout>>,
    /// Every line the server wrote, for the key check.
    seen: Vec<String>,
    next_id: u64,
}

impl Sidecar {
    fn spawn() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_linch-server"))
            .env("DEEPSEEK_API_KEY", SECRET)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let stdin = child.stdin.take();
        let stdout = BufReader::new(child.stdout.take().unwrap()).lines();
        Self {
            child,
            stdin,
            stdout,
            seen: Vec::new(),
            next_id: 0,
        }
    }

    async fn line(&mut self) -> Value {
        let read = timeout(Duration::from_secs(20), self.stdout.next_line());
        let line = read.await.expect("server silent").unwrap().expect("eof");
        let value = serde_json::from_str(&line).unwrap();
        self.seen.push(line);
        value
    }

    /// Send one call and return its response; notifications stay unread.
    async fn call(&mut self, method: &str, params: Value) -> Value {
        self.next_id += 1;
        let request = json!({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                             "params": params});
        let stdin = self.stdin.as_mut().unwrap();
        stdin
            .write_all(format!("{request}\n").as_bytes())
            .await
            .unwrap();
        stdin.flush().await.unwrap();
        let response = self.line().await;
        assert_eq!(response["id"], self.next_id, "{response}");
        response
    }

    /// The run's envelopes until `run.end`, which must carry no error.
    async fn until_end(&mut self) -> Vec<Value> {
        let mut events = Vec::new();
        loop {
            let notification = self.line().await;
            match notification["method"].as_str() {
                Some("run.event") => {
                    let envelopes = notification["params"]["envelopes"].as_array().unwrap();
                    events.extend(envelopes.iter().map(|e| e["event"].clone()));
                }
                Some("run.end") => {
                    assert!(
                        notification["params"].get("error").is_none(),
                        "{notification}"
                    );
                    return events;
                }
                _ => panic!("unexpected line {notification}"),
            }
        }
    }

    async fn run(&mut self, config: Value) -> Vec<Value> {
        let opened = self.call("agent.open", config).await;
        let agent = opened["result"]["agent"].clone();
        let params = json!({"agent": agent, "session_id": "s1", "prompt": "hello"});
        let started = self.call("run.start", params).await;
        assert!(started["result"]["run_id"].is_string(), "{started}");
        self.until_end().await
    }
}

#[tokio::test]
async fn the_binary_serves_runs_over_stdio_and_never_echoes_keys() {
    let mut server = Sidecar::spawn();
    let turn = TurnBuilder::new("scripted-model").text("hi there").build();
    let scripted = json!({"namespace": "stdio", "spec": {"model": "scripted-model"},
                          "provider": {"id": "scripted", "turns": [turn]}});
    let events = server.run(scripted).await;
    assert_eq!(events[0]["type"], "init");
    let result = events.last().unwrap();
    assert_eq!(result["type"], "result");
    assert_eq!(result["subtype"], "success");
    assert_eq!(result["final_text"], "hi there");

    let rejected = json!({"namespace": "stdio", "spec": {"model": "m"},
                          "provider": {"id": "deepseek", "api_key": "sk-from-the-client"}});
    let refused = server.call("agent.open", rejected).await;
    assert_eq!(refused["error"]["code"], linch_rpc::code::INVALID_PARAMS);

    let deepseek = json!({"namespace": "stdio", "spec": {"model": "deepseek-flash"},
                          "provider": {"id": "deepseek", "base_url": "http://127.0.0.1:1"}});
    let events = server.run(deepseek).await;
    let result = events.last().unwrap();
    assert_eq!(result["type"], "result");
    assert_eq!(result["subtype"], "error", "{result}");
    // The request was really sent, with the key, and failed.
    let error = &events[events.len() - 2];
    assert_eq!(error["type"], "error", "{error}");
    assert_eq!(error["error"]["kind"], "transport", "{error}");

    drop(server.stdin.take());
    let status = timeout(Duration::from_secs(20), server.child.wait())
        .await
        .expect("server did not exit after stdin closed")
        .unwrap();
    assert!(status.success(), "{status}");
    let mut stderr = String::new();
    let mut pipe = server.child.stderr.take().unwrap();
    pipe.read_to_string(&mut stderr).await.unwrap();
    for text in server.seen.iter().chain([&stderr]) {
        assert!(!text.contains(SECRET), "the key leaked: {text}");
        assert!(
            !text.contains("sk-from-the-client"),
            "a client key echoed: {text}"
        );
    }
}
