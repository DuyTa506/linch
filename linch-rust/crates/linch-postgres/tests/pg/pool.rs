//! The pool `connect` opens.

/// A run commit or session append is one round trip; sqlx's default ping
/// before every acquire would make it two.
#[tokio::test]
async fn connect_skips_the_ping_before_each_acquire() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
        return;
    };
    let pool = linch_postgres::connect(&dsn, linch_postgres::CONNECT_TIMEOUT)
        .await
        .expect("connect to the test database");
    assert!(!pool.options().get_test_before_acquire());
    pool.close().await;
}
