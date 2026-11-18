//! A store call that waits out the pool's acquire timeout says so, and says
//! what to do about it.

use std::time::Duration;

use linch_core::{SessionId, SessionStore};
use linch_postgres::{ACQUIRE_TIMEOUT, PgSessionStore, is_pool_exhausted};
use sqlx::postgres::PgPoolOptions;

use crate::common::TestDb;

#[tokio::test]
async fn a_call_that_waits_out_the_acquire_timeout_names_the_remedy() {
    let Some(db) = TestDb::migrated("pool_exhausted").await else {
        return;
    };
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_millis(200))
        .connect_with(db.options.clone())
        .await
        .unwrap();
    let _held = pool.acquire().await.unwrap();

    let store = PgSessionStore::new(pool.clone());
    let error = store.create("ns", &SessionId::new("s1")).await.unwrap_err();

    assert!(is_pool_exhausted(&error), "{error:?}");
    let text = error.to_string();
    assert!(
        std::error::Error::source(&error).is_some_and(|source| source.to_string()
            == "pool timed out after 30s: raise max_pool_connections or lower concurrent runs; a run already created stays at its last durable state: resume it"),
        "{text} / {:?}",
        std::error::Error::source(&error)
    );
    drop(_held);
    pool.close().await;
    db.finish().await;
}

#[tokio::test]
async fn a_connected_pool_waits_the_30s_the_message_names() {
    let Some(db) = TestDb::migrated("pool_acquire").await else {
        return;
    };
    assert_eq!(ACQUIRE_TIMEOUT, Duration::from_secs(30));
    let dsn = std::env::var("LINCH_TEST_PG_DSN").unwrap();
    let pool = linch_postgres::connect(
        &dsn,
        linch_postgres::CONNECT_TIMEOUT,
        linch_postgres::default_max_connections(),
    )
    .await
    .unwrap();
    assert_eq!(pool.options().get_acquire_timeout(), ACQUIRE_TIMEOUT);
    pool.close().await;
    db.finish().await;
}
