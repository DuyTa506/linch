//! The pool `connect` opens.

use std::num::NonZeroU32;

/// A run commit or session append is one round trip; sqlx's default ping
/// before every acquire would make it two. `connect` pings only a
/// connection that sat idle (`idle.rs`).
#[tokio::test]
async fn connect_turns_off_the_ping_before_every_acquire() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
        return;
    };
    let max = linch_postgres::default_max_connections();
    let pool = linch_postgres::connect(&dsn, linch_postgres::CONNECT_TIMEOUT, max)
        .await
        .expect("connect to the test database");
    assert!(!pool.options().get_test_before_acquire());
    assert_eq!(pool.options().get_max_connections(), max.get());
    pool.close().await;
}

#[tokio::test]
async fn connect_sizes_the_pool() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
        return;
    };
    let three = NonZeroU32::new(3).unwrap();
    let pool = linch_postgres::connect(&dsn, linch_postgres::CONNECT_TIMEOUT, three)
        .await
        .expect("connect to the test database");
    assert_eq!(pool.options().get_max_connections(), 3);
    pool.close().await;
}
