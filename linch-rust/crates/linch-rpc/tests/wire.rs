//! The raw wire: one JSON line in, one JSON line out, and every framing
//! and strictness rule of the protocol document.

mod common;

use common::{scripted, text_turn};
use linch_rpc::code;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream, Lines, WriteHalf};

/// Raw lines to and from an in-process server.
struct Wire {
    input: WriteHalf<DuplexStream>,
    output: Lines<BufReader<tokio::io::ReadHalf<DuplexStream>>>,
}

impl Wire {
    fn new() -> Self {
        let (ours, theirs) = tokio::io::duplex(64 * 1024);
        let (server_reader, server_writer) = tokio::io::split(theirs);
        tokio::spawn(linch_rpc::serve(server_reader, server_writer));
        let (reader, input) = tokio::io::split(ours);
        Self {
            input,
            output: BufReader::new(reader).lines(),
        }
    }

    async fn send(&mut self, line: &str) {
        self.input.write_all(line.as_bytes()).await.unwrap();
        self.input.write_all(b"\n").await.unwrap();
    }

    async fn recv(&mut self) -> Value {
        let line = self
            .output
            .next_line()
            .await
            .unwrap()
            .expect("server closed");
        serde_json::from_str(&line).unwrap()
    }

    /// Send `line` and return the error code and id of the answer.
    async fn error(&mut self, line: &str) -> (i64, Value) {
        self.send(line).await;
        let answer = self.recv().await;
        assert_eq!(answer["jsonrpc"], "2.0", "{answer}");
        assert!(answer.get("result").is_none(), "{answer}");
        let code = answer["error"]["code"].as_i64().unwrap();
        (code, answer["id"].clone())
    }
}

#[tokio::test]
async fn malformed_requests_get_the_json_rpc_codes() {
    let mut wire = Wire::new();
    let cases = [
        ("{not json", code::PARSE_ERROR, json!(null)),
        ("[]", code::INVALID_REQUEST, json!(null)),
        (
            r#"[{"jsonrpc":"2.0","id":1,"method":"run.cancel"}]"#,
            code::INVALID_REQUEST,
            json!(null),
        ),
        ("7", code::INVALID_REQUEST, json!(null)),
        (
            r#"{"jsonrpc":"1.0","id":1,"method":"run.cancel"}"#,
            code::INVALID_REQUEST,
            json!(1),
        ),
        (
            r#"{"id":"a","method":"run.cancel"}"#,
            code::INVALID_REQUEST,
            json!("a"),
        ),
        (
            r#"{"jsonrpc":"2.0","id":2,"method":"run.cancel","extra":0}"#,
            code::INVALID_REQUEST,
            json!(2),
        ),
        (
            r#"{"jsonrpc":"2.0","id":{},"method":"run.cancel"}"#,
            code::INVALID_REQUEST,
            json!(null),
        ),
        (
            r#"{"jsonrpc":"2.0","id":3,"method":7}"#,
            code::INVALID_REQUEST,
            json!(3),
        ),
        (
            r#"{"jsonrpc":"2.0","id":4,"method":"run.cancel","params":["r"]}"#,
            code::INVALID_PARAMS,
            json!(4),
        ),
        (
            r#"{"jsonrpc":"2.0","id":5,"method":"run.nope","params":{}}"#,
            code::METHOD_NOT_FOUND,
            json!(5),
        ),
        (
            r#"{"jsonrpc":"2.0","id":6,"method":"run.cancel","params":{}}"#,
            code::INVALID_PARAMS,
            json!(6),
        ),
        (
            r#"{"jsonrpc":"2.0","id":7,"method":"run.cancel","params":{"run_id":"r","x":1}}"#,
            code::INVALID_PARAMS,
            json!(7),
        ),
    ];
    for (line, expected, id) in cases {
        assert_eq!(wire.error(line).await, (expected, id), "{line}");
    }
}

#[tokio::test]
async fn an_agent_config_with_a_key_or_unknown_field_is_refused() {
    let mut wire = Wire::new();
    let mut with_key = scripted(vec![], None, "");
    with_key["provider"] = json!({"id": "deepseek", "api_key": "sk-from-the-client"});
    let mut extra = scripted(vec![], None, "");
    extra["spec"]["tools"] = json!(["Read"]);
    for (id, params) in [with_key, extra].into_iter().enumerate() {
        let request = json!({"jsonrpc": "2.0", "id": id, "method": "agent.open", "params": params});
        let (got, _) = wire.error(&request.to_string()).await;
        assert_eq!(got, code::INVALID_PARAMS);
    }
}

#[tokio::test]
async fn a_notification_gets_no_answer_and_blank_lines_are_skipped() {
    let mut wire = Wire::new();
    wire.send(r#"{"jsonrpc":"2.0","method":"run.cancel","params":{"run_id":"r"}}"#)
        .await;
    wire.send("").await;
    wire.send(r#"{"jsonrpc":"2.0","id":"next","method":"run.cancel","params":{"run_id":"r"}}"#)
        .await;
    let answer = wire.recv().await;
    assert_eq!(
        answer,
        json!({"jsonrpc": "2.0", "id": "next", "result": {"cancelled": false}})
    );
}

#[tokio::test]
async fn the_start_result_comes_before_the_run_notifications() {
    let mut wire = Wire::new();
    let config = scripted(vec![text_turn("hi")], None, "");
    let open = json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open", "params": config});
    wire.send(&open.to_string()).await;
    let opened = wire.recv().await;
    let agent = opened["result"]["agent"].clone();
    let start = json!({"jsonrpc": "2.0", "id": 2, "method": "run.start",
                       "params": {"agent": agent, "session_id": "s1", "prompt": "hello"}});
    wire.send(&start.to_string()).await;
    let accepted = wire.recv().await;
    assert_eq!(accepted["id"], 2, "{accepted}");
    let run_id = accepted["result"]["run_id"].clone();
    let mut methods = Vec::new();
    loop {
        let notification = wire.recv().await;
        assert!(notification.get("id").is_none(), "{notification}");
        let method = notification["method"].as_str().unwrap().to_owned();
        assert_eq!(notification["params"]["run_id"], run_id);
        methods.push(method.clone());
        if method == "run.end" {
            assert!(notification["params"].get("error").is_none());
            break;
        }
        assert_eq!(notification["params"]["v"], 1);
    }
    assert!(
        methods[..methods.len() - 1]
            .iter()
            .all(|m| m == "run.event")
    );
}
