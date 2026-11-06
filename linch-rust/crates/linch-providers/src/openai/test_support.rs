//! Test helpers shared by the OpenAI-family adapters: request builders, a
//! one-shot local HTTP server and SSE fixture runners.

use std::convert::Infallible;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread::{self, JoinHandle};

use futures::{StreamExt, stream};
use linch_core::{
    AssistantAssembly, Message, ProviderEvent, ProviderRequest, ProviderStream, ToolDefinition,
    assemble,
};
use serde_json::json;

/// A minimal request for `model` with one user turn.
pub(crate) fn request(model: &str) -> ProviderRequest {
    ProviderRequest {
        model: model.into(),
        system: vec![],
        tools: vec![],
        messages: vec![Message::user_text("hi")],
        max_output_tokens: None,
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

/// A one-parameter weather tool.
pub(crate) fn weather_tool() -> ToolDefinition {
    ToolDefinition {
        name: "get_weather".into(),
        description: "Weather.".into(),
        input_schema: json!({"type": "object"}).as_object().unwrap().clone(),
    }
}

/// `sse` as a byte stream split into `chunk`-byte pieces.
pub(crate) fn bytes(
    sse: &str,
    chunk: usize,
) -> impl futures::Stream<Item = Result<Vec<u8>, Infallible>> + Send + 'static {
    let pieces: Vec<Result<Vec<u8>, Infallible>> = sse
        .as_bytes()
        .chunks(chunk)
        .map(|piece| Ok(piece.to_vec()))
        .collect();
    stream::iter(pieces)
}

/// Collect a stream that must not fail.
pub(crate) async fn collect_ok(events: ProviderStream) -> Vec<ProviderEvent> {
    events
        .collect::<Vec<_>>()
        .await
        .into_iter()
        .collect::<Result<_, _>>()
        .expect("stream has no error")
}

/// Collect a stream and assemble it into one turn.
pub(crate) async fn assemble_ok(events: ProviderStream) -> AssistantAssembly {
    assemble(collect_ok(events).await).expect("valid stream")
}

/// Serve one canned HTTP response on a local port. Returns the base URL and
/// a handle that yields the raw request (head and body) it read.
pub(crate) fn serve_once(response: String) -> (String, JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        let (socket, _) = listener.accept().unwrap();
        let mut reader = BufReader::new(socket);
        let mut head = String::new();
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse().unwrap();
            }
            head.push_str(&line);
            if line == "\r\n" {
                break;
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        reader.get_mut().write_all(response.as_bytes()).unwrap();
        head + &String::from_utf8(body).unwrap()
    });
    (url, handle)
}

/// A `200 OK` SSE response carrying `sse`.
pub(crate) fn sse_response(sse: &str) -> String {
    format!("HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}")
}

/// An error response with `status` (for example `429 Too Many Requests`),
/// extra `headers` lines (each ending in `\r\n`) and `body`.
pub(crate) fn error_response(status: &str, headers: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status}\r\n{headers}content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    )
}
