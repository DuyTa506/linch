//! The pool `connect` opens.

use std::num::NonZeroU32;

/// A run commit or session append is one round trip; sqlx's default ping
/// before every acquire would make it two.
#[tokio::test]
async fn connect_skips_the_ping_before_each_acquire() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
        return;
    };
    let pool = linch_postgres::connect(
        &dsn,
        linch_postgres::CONNECT_TIMEOUT,
        linch_postgres::MAX_CONNECTIONS,
    )
    .await
    .expect("connect to the test database");
    assert!(!pool.options().get_test_before_acquire());
    assert_eq!(linch_postgres::MAX_CONNECTIONS.get(), 10, "sqlx's default");
    assert_eq!(pool.options().get_max_connections(), 10);
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
