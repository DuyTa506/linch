//! `provider.capabilities` is keyless: the server never reads the provider's
//! key from its environment for it, so the one probe of a self-hosted server
//! goes out without an `Authorization` header, while the calls that need the
//! key (`provider.models`, an `agent.open`) still send it.
//!
//! The key is set in the environment of a spawned `linch-server`, so the
//! test needs no `set_var` in its own process.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{ChildStdin, ChildStdout, Command};
use tokio::time::timeout;

const SECRET: &str = "sk-linch-keyless-test-secret-5150";

/// A server that answers every request with a one-model list, keeping the
/// head of each request it got, lower-cased. Returns its `/v1` URL.
fn serve_models() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/v1", listener.local_addr().unwrap());
    let body = json!({"object": "list", "data": [
        {"id": "qwen", "object": "model", "max_model_len": 4096}]})
    .to_string();
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    );
    let seen = Arc::new(Mutex::new(Vec::new()));
    let heads = Arc::clone(&seen);
    thread::spawn(move || {
        for socket in listener.incoming() {
            let Ok(mut socket) = socket else { return };
            let mut buffer = [0; 4096];
            let read = socket.read(&mut buffer).unwrap_or(0);
            let head = String::from_utf8_lossy(&buffer[..read]).to_lowercase();
            heads.lock().unwrap().push(head);
            let _ = socket.write_all(response.as_bytes());
        }
    });
    (url, seen)
}

/// One request at a time over the server's stdio.
struct Wire {
    stdin: Option<ChildStdin>,
    lines: Lines<BufReader<ChildStdout>>,
    next_id: u64,
}

impl Wire {
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
        let line = timeout(Duration::from_secs(20), self.lines.next_line());
        let line = line.await.expect("server silent").unwrap().expect("eof");
        let response: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(response["id"], self.next_id, "{response}");
        response
    }
}

#[tokio::test]
async fn capabilities_never_send_the_key_the_calls_that_need_it_do() {
    let (url, seen) = serve_models();
    let mut child = Command::new(env!("CARGO_BIN_EXE_linch-server"))
        .env("VLLM_API_KEY", SECRET)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut server = Wire {
        stdin: child.stdin.take(),
        lines: BufReader::new(child.stdout.take().unwrap()).lines(),
        next_id: 0,
    };
    let provider = json!({"id": "vllm", "base_url": url});

    let params = json!({"provider": provider, "model": "qwen"});
    let caps = server.call("provider.capabilities", params).await;
    // The probe ran (the window is the server's), and it carried no key.
    assert_eq!(caps["result"]["context_window"], 4096, "{caps}");
    assert_eq!(caps["result"]["context_source"], "server", "{caps}");
    let heads = seen.lock().unwrap().clone();
    assert_eq!(heads.len(), 1, "{heads:?}");
    assert!(
        !heads[0].contains("authorization"),
        "a keyless call sent a header: {}",
        heads[0]
    );
    assert!(!heads[0].contains(&SECRET.to_lowercase()), "{}", heads[0]);

    // The calls that need the key send it, so the check above can fail.
    let listed = server
        .call("provider.models", json!({"provider": provider}))
        .await;
    assert_eq!(listed["result"]["models"][0]["id"], "qwen", "{listed}");
    let heads = seen.lock().unwrap().clone();
    let bearer = format!("authorization: bearer {}", SECRET.to_lowercase());
    assert!(heads[1].contains(&bearer), "{}", heads[1]);

    drop(server.stdin.take());
    let status = timeout(Duration::from_secs(20), child.wait()).await;
    assert!(status.expect("server exited").unwrap().success());
}
