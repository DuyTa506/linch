//! A one-shot local HTTP server for adapter round-trip tests.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread::{self, JoinHandle};

use linch_core::ProviderError;

/// A `307` whose `location` is a local port nothing listens on, so a
/// client that follows it fails to connect instead of reaching a server.
pub(crate) fn redirect_response() -> String {
    let closed = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap();
    format!(
        "HTTP/1.1 307 Temporary Redirect\r\nlocation: http://{closed}/elsewhere\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
    )
}

/// A `400` that promises a 10 MB body starting with `prefix`, sends 64 KiB
/// of it and then stalls until the client hangs up. A client that reads the
/// whole body before answering never returns.
pub(crate) fn serve_stalled_error(prefix: &str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let response = format!(
        "HTTP/1.1 400 Bad Request\r\ncontent-length: 10000000\r\n\r\n{prefix}{}",
        "x".repeat(64 * 1024)
    );
    thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        let mut buffer = [0; 4096];
        let mut seen = Vec::new();
        while !seen.windows(4).any(|window| window == b"\r\n\r\n") {
            match socket.read(&mut buffer) {
                Ok(0) | Err(_) => return,
                Ok(read) => seen.extend_from_slice(&buffer[..read]),
            }
        }
        // The client may hang up mid-write once it has read enough.
        let _ = socket.write_all(response.as_bytes());
        while matches!(socket.read(&mut buffer), Ok(read) if read > 0) {}
    });
    url
}

/// The call failed on the redirect itself rather than following it.
pub(crate) fn assert_redirect_refused(err: &ProviderError) {
    assert!(
        matches!(err, ProviderError::Status { status: 307, retry_after: None, message }
            if message == crate::transport::REDIRECT_REFUSED),
        "{err:?}"
    );
    assert!(!err.retryable());
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
