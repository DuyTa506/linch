//! Connecting with a timeout: an unreachable server fails fast instead of
//! waiting out sqlx's 30 s acquire timeout. No database is needed.

use std::time::{Duration, Instant};

#[tokio::test]
async fn an_unreachable_server_fails_within_the_timeout() {
    // A port that was free a moment ago refuses connections, which sqlx
    // retries until its deadline.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let dsn = format!("postgres://postgres:secret@127.0.0.1:{port}/linch");

    let started = Instant::now();
    let timeout = Duration::from_millis(300);
    let error = linch_postgres::connect(&dsn, timeout, linch_postgres::default_max_connections())
        .await
        .expect_err("nothing listens there");
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "{:?}",
        started.elapsed()
    );
    let sqlx::Error::Io(io) = &error else {
        panic!("a timeout, got {error:?}");
    };
    assert_eq!(io.kind(), std::io::ErrorKind::TimedOut);
    let text = error.to_string();
    assert!(
        text.contains(&format!("Postgres at 127.0.0.1:{port} within 300ms")),
        "names the host, port and wait: {text}"
    );
    assert!(!text.contains("secret"), "{error}");
}

#[tokio::test]
async fn a_malformed_dsn_fails_at_once() {
    let error = linch_postgres::connect(
        "not a dsn",
        linch_postgres::CONNECT_TIMEOUT,
        linch_postgres::default_max_connections(),
    )
    .await
    .expect_err("not a DSN");
    assert!(matches!(error, sqlx::Error::Configuration(_)), "{error:?}");
}
