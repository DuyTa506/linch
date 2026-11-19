//! Spawns the `linch-server` binary and speaks the protocol over its stdio:
//! a scripted run end to end, then a DeepSeek run against a closed local
//! port whose key, set in the server's environment, must never come back.
//! Closing stdin stops the server; the test waits for it to exit. Pipes
//! the server shares with other processes stay blocking.

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
        Self::spawn_with(&[])
    }

    fn spawn_with(env: &[(&str, &str)]) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_linch-server"))
            .env_remove("LINCH_LOG")
            .env_remove("RUST_LOG")
            .envs(env.iter().copied())
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

    /// [`Self::call`], reading past the notifications (`workflow.event`)
    /// that come before the response.
    async fn call_through(&mut self, method: &str, params: Value) -> Value {
        self.next_id += 1;
        let request = json!({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                             "params": params});
        let stdin = self.stdin.as_mut().unwrap();
        stdin
            .write_all(format!("{request}\n").as_bytes())
            .await
            .unwrap();
        stdin.flush().await.unwrap();
        loop {
            let line = self.line().await;
            if line["id"] == self.next_id {
                return line;
            }
        }
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

    // Retries off, so the one failed call ends the run.
    let deepseek = json!({"namespace": "stdio",
                          "spec": {"model": "deepseek-flash", "provider_retry": {"max_attempts": 1}},
                          "provider": {"id": "deepseek", "base_url": "http://127.0.0.1:1"}});
    let events = server.run(deepseek).await;
    let result = events.last().unwrap();
    assert_eq!(result["type"], "result");
    assert_eq!(result["subtype"], "error", "{result}");
    // The call really went out, with the key, and found nothing listening.
    let error = &events[events.len() - 2];
    assert_eq!(error["type"], "error", "{error}");
    assert_eq!(error["error"]["kind"], "connect", "{error}");

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

/// A workflow call whose agent cannot reach its provider: the server logs
/// `a workflow call failed` with `tracing::warn!`. Returns what the server
/// wrote to stderr once it exited; stdout carried only protocol lines.
async fn stderr_after_a_failed_workflow_call(env: &[(&str, &str)]) -> String {
    let mut server = Sidecar::spawn_with(env);
    let deepseek = json!({"namespace": "stdio",
                          "spec": {"model": "deepseek-flash", "provider_retry": {"max_attempts": 1}},
                          "provider": {"id": "deepseek", "base_url": "http://127.0.0.1:1"}});
    let opened = server.call("agent.open", deepseek).await;
    let agent = opened["result"]["agent"].clone();
    let started = server
        .call_through("workflow.start", json!({"agent": agent, "run_id": "w"}))
        .await;
    assert!(started["result"].is_object(), "{started}");
    let failed = server
        .call_through("workflow.agent", json!({"run_id": "w", "prompt": "hi"}))
        .await;
    assert_eq!(
        failed["error"]["code"],
        linch_rpc::code::WORKFLOW_CALL_FAILED,
        "{failed}"
    );
    server
        .call_through("workflow.finish", json!({"run_id": "w", "ok": false}))
        .await;
    drop(server.stdin.take());
    let status = timeout(Duration::from_secs(20), server.child.wait())
        .await
        .expect("server did not exit after stdin closed")
        .unwrap();
    assert!(status.success(), "{status}");
    let mut stderr = String::new();
    let mut pipe = server.child.stderr.take().unwrap();
    pipe.read_to_string(&mut stderr).await.unwrap();
    assert!(!stderr.contains(SECRET), "the key leaked: {stderr}");
    stderr
}

/// The server's `tracing` records go to stderr (warnings by default), and
/// `LINCH_LOG` filters them: without a subscriber every `tracing::warn!`
/// went nowhere.
#[tokio::test]
async fn the_servers_log_goes_to_stderr_and_linch_log_filters_it() {
    let stderr = stderr_after_a_failed_workflow_call(&[]).await;
    assert!(stderr.contains("a workflow call failed"), "{stderr}");
    assert!(stderr.contains("WARN"), "{stderr}");

    let quiet = stderr_after_a_failed_workflow_call(&[("LINCH_LOG", "error")]).await;
    assert!(!quiet.contains("a workflow call failed"), "{quiet}");
    // RUST_LOG is read when LINCH_LOG is not set.
    let off = stderr_after_a_failed_workflow_call(&[("RUST_LOG", "off")]).await;
    assert!(!off.contains("a workflow call failed"), "{off}");
}

/// Stdio that is not a pipe (files here) falls back to tokio's stdin and
/// stdout and still serves until the input ends.
#[tokio::test]
async fn the_binary_also_serves_files_as_stdin_and_stdout() {
    let dir = tempfile::tempdir().unwrap();
    let (input, output) = (dir.path().join("in"), dir.path().join("out"));
    let calls = [
        r#"{"jsonrpc":"2.0","id":1,"method":"run.cancel","params":{"run_id":"r"}}"#,
        r#"{"jsonrpc":"2.0","id":2,"method":"run.cancel","params":{"run_id":"s"}}"#,
    ];
    std::fs::write(&input, calls.join("\n") + "\n").unwrap();
    let status = Command::new(env!("CARGO_BIN_EXE_linch-server"))
        .stdin(std::fs::File::open(&input).unwrap())
        .stdout(std::fs::File::create(&output).unwrap())
        .status();
    let status = timeout(Duration::from_secs(20), status).await.unwrap();
    assert!(status.unwrap().success());
    let text = std::fs::read_to_string(&output).unwrap();
    let mut ids: Vec<Value> = (text.lines())
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .inspect(|answer| assert_eq!(answer["result"], json!({"cancelled": false})))
        .map(|answer| answer["id"].clone())
        .collect();
    ids.sort_by_key(Value::as_u64);
    assert_eq!(ids, [json!(1), json!(2)], "{text}");
}

/// Whether the open file description behind `fd` is non-blocking, from
/// `/proc/self/fdinfo` (`O_NONBLOCK` is `0o4000` on x86-64 and arm64).
#[cfg(target_os = "linux")]
fn nonblocking(fd: &impl std::os::fd::AsRawFd) -> bool {
    let info = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", fd.as_raw_fd())).unwrap();
    let flags = (info.lines())
        .find_map(|line| line.strip_prefix("flags:"))
        .expect("a flags line");
    u32::from_str_radix(flags.trim(), 8).unwrap() & 0o4000 != 0
}

/// A shell's `producer | { linch-server; next-step; } | consumer` hands
/// the server pipes the next step reads and writes too: the server serves
/// them as pipes and leaves them blocking, so that step gets no `EAGAIN`.
#[cfg(target_os = "linux")]
#[tokio::test]
async fn the_binary_leaves_the_pipes_it_shares_blocking() {
    use std::io::{Read, Write};

    let (stdin, mut feed) = std::io::pipe().unwrap();
    let (mut answers, stdout) = std::io::pipe().unwrap();
    assert!(!nonblocking(&stdin) && !nonblocking(&stdout));
    let call = r#"{"jsonrpc":"2.0","id":1,"method":"run.cancel","params":{"run_id":"r"}}"#;
    feed.write_all(format!("{call}\n").as_bytes()).unwrap();
    drop(feed);
    // The test keeps its own handle on each pipe, as the shell does.
    let status = Command::new(env!("CARGO_BIN_EXE_linch-server"))
        .stdin(stdin.try_clone().unwrap())
        .stdout(stdout.try_clone().unwrap())
        .status();
    let status = timeout(Duration::from_secs(20), status).await.unwrap();
    assert!(status.unwrap().success());
    assert!(!nonblocking(&stdin), "stdin's pipe was left non-blocking");
    assert!(!nonblocking(&stdout), "stdout's pipe was left non-blocking");
    drop(stdout);
    let mut text = String::new();
    answers.read_to_string(&mut text).unwrap();
    let answer: Value = serde_json::from_str(text.trim()).unwrap();
    assert_eq!(answer["result"], json!({"cancelled": false}), "{text}");
}

/// Threads of the server process, from `/proc`; `None` off Linux.
fn thread_count(server: &Sidecar) -> Option<usize> {
    let pid = server.child.id()?;
    let tasks = std::fs::read_dir(format!("/proc/{pid}/task")).ok()?;
    Some(tasks.count())
}

#[tokio::test]
async fn stdio_threads_env_selects_worker_threads() {
    let mut server = Sidecar::spawn_with(&[("LINCH_STDIO_THREADS", "4")]);
    server.call("server.info", json!({})).await;
    let Some(threads) = thread_count(&server) else {
        return;
    };
    assert!(threads >= 5, "{threads} threads");
}

#[tokio::test]
async fn stdio_without_the_variable_keeps_one_runtime_thread() {
    for env in [&[][..], &[("LINCH_STDIO_THREADS", "1")][..]] {
        let mut server = Sidecar::spawn_with(env);
        server.call("server.info", json!({})).await;
        let Some(threads) = thread_count(&server) else {
            return;
        };
        assert!(threads <= 2, "{env:?}: {threads} threads");
    }
}

#[tokio::test]
async fn a_bad_stdio_threads_value_fails_at_startup_naming_the_variable() {
    for bad in ["many", "0", "257", "-3", ""] {
        let mut server = Sidecar::spawn_with(&[("LINCH_STDIO_THREADS", bad)]);
        let status = timeout(Duration::from_secs(20), server.child.wait())
            .await
            .expect("server kept running")
            .unwrap();
        assert!(!status.success(), "{bad:?}: {status}");
        let mut stderr = String::new();
        let mut pipe = server.child.stderr.take().unwrap();
        pipe.read_to_string(&mut stderr).await.unwrap();
        assert!(stderr.contains("LINCH_STDIO_THREADS"), "{bad:?}: {stderr}");
    }
}
