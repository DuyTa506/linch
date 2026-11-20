//! The connection that renews leases: bounded by its patience, never queued
//! behind run work, and replaced after it breaks.

use std::time::{Duration, Instant};

use linch_core::{RunStore, StoreError};
use linch_postgres::PgRunStore;
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

use crate::common::{TestDb, new_run};

/// A lease of 3 s: a renewal gives up after 1 s.
const TTL: Duration = Duration::from_secs(3);

/// A pool of 2 whose sessions carry an application name, so a test finds
/// the sessions it opened.
async fn pool_named(db: &TestDb, name: &str) -> PgPool {
    let options = db.options.clone().application_name(name);
    PgPoolOptions::new()
        .max_connections(2)
        .connect_with(options)
        .await
        .unwrap()
}

/// A renewal blocked on the run's row lock fails in about a third of the
/// lease with a backend error (not `LeaseLost`), leaves no backend waiting
/// behind it, and works again once the lock is released.
#[tokio::test]
async fn a_renewal_blocked_on_a_row_lock_gives_up_within_its_patience() {
    let Some(db) = TestDb::migrated("line_lock").await else {
        return;
    };
    let store = PgRunStore::new(pool_named(&db, "line_lock").await).with_lease_ttl(TTL);
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    store.create(run).await.unwrap();
    let lease = store.claim("ns", &id, "a").await.unwrap();
    store.renew(&lease).await.unwrap();

    let mut lock = db.admin.begin().await.unwrap();
    let sql = format!("SELECT 1 FROM {}.linch_runs FOR UPDATE", db.schema);
    sqlx::query(AssertSqlSafe(sql))
        .execute(&mut *lock)
        .await
        .unwrap();
    let started = Instant::now();
    let renewed = store.renew(&lease).await;
    let waited = started.elapsed();
    assert!(
        matches!(renewed, Err(StoreError::Backend(_))),
        "{renewed:?}"
    );
    assert!(waited < TTL / 3 + Duration::from_millis(500), "{waited:?}");

    // The server cancelled the statement too: nothing waits on the lock.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let waiting: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_stat_activity \
         WHERE wait_event_type = 'Lock' AND application_name = 'line_lock'",
    )
    .fetch_one(&db.admin)
    .await
    .unwrap();
    assert_eq!(waiting, 0, "a renewal still waits for the lock");

    lock.rollback().await.unwrap();
    store.renew(&lease).await.unwrap();
    db.finish().await;
}

/// A renewal needs no connection of the pool: with its pool all busy, it
/// still lands.
#[tokio::test]
async fn a_renewal_does_not_use_the_pool() {
    let Some(db) = TestDb::migrated("line_pool").await else {
        return;
    };
    let pool = pool_named(&db, "line_pool").await;
    let store = PgRunStore::new(pool.clone()).with_lease_ttl(TTL);
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    store.create(run).await.unwrap();
    let lease = store.claim("ns", &id, "a").await.unwrap();
    store.renew(&lease).await.unwrap();
    let held = [pool.acquire().await.unwrap(), pool.acquire().await.unwrap()];
    store.renew(&lease).await.unwrap();
    store.release(&lease).await.unwrap();
    drop(held);
    db.finish().await;
}

/// A connection the server closed is replaced by the next renewal itself.
#[tokio::test]
async fn a_killed_connection_is_replaced_by_the_next_renewal() {
    let Some(db) = TestDb::migrated("line_killed").await else {
        return;
    };
    let store = PgRunStore::new(pool_named(&db, "line_killed").await).with_lease_ttl(TTL);
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    store.create(run).await.unwrap();
    let lease = store.claim("ns", &id, "a").await.unwrap();
    store.renew(&lease).await.unwrap();

    let killed: Vec<bool> = sqlx::query_scalar(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE application_name = 'line_killed'",
    )
    .fetch_all(&db.admin)
    .await
    .unwrap();
    assert!(killed.len() >= 2, "the lease connection was not found");
    store.renew(&lease).await.unwrap();
    db.finish().await;
}

/// A renewal that lands after the holder released its run (the heartbeat
/// task is aborted with a renewal in flight and the server still executes
/// it) must not lease the run again: the release exists so that another
/// process need not wait out the TTL.
#[tokio::test]
async fn a_renewal_after_the_release_leaves_the_run_free() {
    let Some(db) = TestDb::migrated("renew_release").await else {
        return;
    };
    let ttl = Duration::from_secs(30);
    let a = PgRunStore::new(db.pool.clone()).with_lease_ttl(ttl);
    let b = PgRunStore::new(db.pool.clone()).with_lease_ttl(ttl);
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    a.create(run).await.unwrap();
    let lease = a.claim("ns", &id, "a").await.unwrap();
    a.release(&lease).await.unwrap();
    // The late renewal of the same lease.
    let late = a.renew(&lease).await;
    let claimed = b.claim("ns", &id, "b").await;
    db.finish().await;
    assert!(
        !matches!(claimed, Err(StoreError::RunLeased { .. })),
        "a renewal after the release leased the run again (renew: {late:?}): {claimed:?}"
    );
}

/// Renewing a held lease still extends it, and an expired lease that was
/// never released still renews while its epoch is current (the epoch is
/// the fence, not the clock).
#[tokio::test]
async fn a_renewal_while_the_lease_is_held_extends_it() {
    let Some(db) = TestDb::migrated("renew_held").await else {
        return;
    };
    let a = PgRunStore::new(db.pool.clone()).with_lease_ttl(Duration::from_secs(30));
    let b = PgRunStore::new(db.pool.clone()).with_lease_ttl(Duration::from_secs(30));
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    a.create(run).await.unwrap();
    let lease = a.claim("ns", &id, "a").await.unwrap();
    a.renew(&lease).await.expect("renew while held");
    let claimed = b.claim("ns", &id, "b").await;
    assert!(
        matches!(claimed, Err(StoreError::RunLeased { .. })),
        "{claimed:?}"
    );
    sqlx::query("UPDATE linch_runs SET lease_until = now() - interval '1 second'")
        .execute(&db.pool)
        .await
        .unwrap();
    let late = a.renew(&lease).await;
    db.finish().await;
    assert!(
        late.is_ok(),
        "an expired lease of the current epoch: {late:?}"
    );
}
