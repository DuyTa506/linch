//! The Postgres tool cache: entries live until their ttl on the database's
//! clock, the oldest go first, namespaces stay apart, a clear is per
//! namespace and refuses a put that began before it, a group's bytes are
//! capped, expired rows of other namespaces are swept, and an agent's cache
//! survives a restart.

use std::time::Duration;

use linch_core::{ByteQuota, PutLimits, ToolCacheStore, ToolOutput};
use linch_postgres::PgToolCache;
use serde_json::json;

use crate::common::TestDb;

const LONG: Duration = Duration::from_secs(60);

fn out(text: &str) -> ToolOutput {
    let mut output = ToolOutput::new(text);
    output.metadata.insert("source".into(), json!("disk"));
    output
}

fn room(ttl: Duration, max_entries: u32) -> PutLimits {
    PutLimits {
        ttl,
        max_entries,
        quota: None,
    }
}

/// Look `key` up, then store `text` at the epoch the lookup saw.
async fn put(cache: &PgToolCache, ns: &str, key: &str, text: &str, limits: &PutLimits) -> bool {
    let epoch = cache.get(ns, key).await.unwrap().epoch;
    cache.put(ns, key, &out(text), epoch, limits).await.unwrap()
}

async fn held(cache: &PgToolCache, ns: &str, key: &str) -> Option<ToolOutput> {
    cache.get(ns, key).await.unwrap().hit
}

#[tokio::test]
async fn an_entry_round_trips_with_its_metadata_and_replaces_by_key() {
    let Some(db) = TestDb::migrated("tool_cache_round").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    assert_eq!(held(&cache, "ns", "k").await, None);
    assert!(put(&cache, "ns", "k", "one", &room(LONG, 8)).await);
    assert_eq!(held(&cache, "ns", "k").await, Some(out("one")));
    assert!(put(&cache, "ns", "k", "two", &room(LONG, 8)).await);
    assert_eq!(held(&cache, "ns", "k").await, Some(out("two")));
    // Another connection of the same database sees it: it is durable.
    let again = PgToolCache::new(db.pool.clone());
    assert_eq!(held(&again, "ns", "k").await, Some(out("two")));
    db.finish().await;
}

#[tokio::test]
async fn an_expired_entry_is_never_returned() {
    let Some(db) = TestDb::migrated("tool_cache_ttl").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    let brief = room(Duration::from_millis(200), 8);
    assert!(put(&cache, "ns", "k", "brief", &brief).await);
    assert!(held(&cache, "ns", "k").await.is_some());
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(held(&cache, "ns", "k").await, None);
    db.finish().await;
}

#[tokio::test]
async fn the_oldest_entries_go_when_a_namespace_is_full() {
    let Some(db) = TestDb::migrated("tool_cache_room").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    for key in ["a", "b", "c"] {
        assert!(put(&cache, "ns", key, key, &room(LONG, 2)).await);
    }
    let kept: Vec<_> = [("a", false), ("b", true), ("c", true)].into();
    for (key, kept) in kept {
        assert_eq!(held(&cache, "ns", key).await.is_some(), kept, "{key}");
    }
    // Rewriting an entry makes it the newest.
    assert!(put(&cache, "ns", "b", "b", &room(LONG, 2)).await);
    assert!(put(&cache, "ns", "d", "d", &room(LONG, 2)).await);
    assert!(held(&cache, "ns", "b").await.is_some());
    assert!(held(&cache, "ns", "c").await.is_none());
    assert!(held(&cache, "ns", "d").await.is_some());
    // One entry of room keeps only the newest.
    assert!(put(&cache, "ns", "e", "e", &room(LONG, 1)).await);
    assert!(held(&cache, "ns", "d").await.is_none());
    assert!(held(&cache, "ns", "e").await.is_some());
    db.finish().await;
}

#[tokio::test]
async fn namespaces_keep_their_entries_apart_and_clear_one_at_a_time() {
    let Some(db) = TestDb::migrated("tool_cache_ns").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    assert!(put(&cache, "acme", "k", "acme", &room(LONG, 1)).await);
    assert!(put(&cache, "other", "k", "other", &room(LONG, 1)).await);
    assert_eq!(held(&cache, "acme", "k").await, Some(out("acme")));
    assert_eq!(held(&cache, "other", "k").await, Some(out("other")));
    cache.clear("acme").await.unwrap();
    assert_eq!(held(&cache, "acme", "k").await, None);
    assert_eq!(held(&cache, "other", "k").await, Some(out("other")));
    db.finish().await;
}

/// A read that began before a write must not store its pre-write answer
/// after the write's clear, and the epoch is never forgotten.
#[tokio::test]
async fn a_put_from_before_a_clear_is_refused() {
    let Some(db) = TestDb::migrated("tool_cache_epoch").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    let before = cache.get("ns", "k").await.unwrap();
    assert_eq!(before.epoch, 0);
    cache.clear("ns").await.unwrap();
    let (old, limits) = (out("old"), room(LONG, 8));
    let stale = cache.put("ns", "k", &old, before.epoch, &limits);
    assert!(!stale.await.unwrap(), "the clear moved the epoch on");
    assert_eq!(held(&cache, "ns", "k").await, None);
    let after = cache.get("ns", "k").await.unwrap();
    assert_eq!(after.epoch, 1);
    let new = out("new");
    assert!(
        cache
            .put("ns", "k", &new, after.epoch, &limits)
            .await
            .unwrap()
    );
    cache.clear("ns").await.unwrap();
    assert_eq!(cache.get("ns", "k").await.unwrap().epoch, 2);
    // Another namespace's epoch is its own.
    assert_eq!(cache.get("other", "k").await.unwrap().epoch, 0);
    db.finish().await;
}

/// A put racing a clear either finishes first and is deleted by it, or
/// sees the new epoch and stores nothing: no entry survives a clear that
/// began after the lookup.
#[tokio::test]
async fn a_put_racing_a_clear_never_survives_it() {
    let Some(db) = TestDb::migrated("tool_cache_race").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    for round in 0..30 {
        let key = format!("k{round}");
        let epoch = cache.get("ns", &key).await.unwrap().epoch;
        let (limits, stale) = (room(LONG, 64), out("stale"));
        let racing = cache.put("ns", &key, &stale, epoch, &limits);
        let (stored, cleared) = tokio::join!(racing, cache.clear("ns"));
        stored.unwrap();
        cleared.unwrap();
        assert_eq!(held(&cache, "ns", &key).await, None, "round {round}");
    }
    db.finish().await;
}

/// A group's namespaces together stay within its bytes, the oldest entry
/// going first; another group and the unquota'd are left alone.
#[tokio::test]
async fn a_quota_bounds_a_group_of_namespaces_together() {
    let Some(db) = TestDb::migrated("tool_cache_quota").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    let size = serde_json::to_string(&out("x")).unwrap().len() as u64;
    let quota = |group: &str| PutLimits {
        ttl: LONG,
        max_entries: 8,
        quota: Some(ByteQuota {
            group: group.into(),
            max_bytes: size * 2,
        }),
    };
    for (ns, key, group) in [
        ("acme/1", "a", "acme"),
        ("acme/2", "b", "acme"),
        ("other", "c", "other"),
        ("acme/1", "d", "acme"),
    ] {
        assert!(put(&cache, ns, key, "x", &quota(group)).await);
    }
    assert_eq!(held(&cache, "acme/1", "a").await, None, "the oldest went");
    assert!(held(&cache, "acme/2", "b").await.is_some());
    assert!(held(&cache, "acme/1", "d").await.is_some());
    assert!(held(&cache, "other", "c").await.is_some());
    db.finish().await;
}

/// A put deletes a bounded batch of expired rows of namespaces nobody
/// writes to any more, so the table does not keep them.
#[tokio::test]
async fn a_put_sweeps_expired_rows_of_other_namespaces() {
    let Some(db) = TestDb::migrated("tool_cache_sweep").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    let brief = room(Duration::from_millis(100), 8);
    for ns in ["gone-1", "gone-2", "gone-3"] {
        assert!(put(&cache, ns, "k", "old", &brief).await);
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    let rows = || async {
        let count = sqlx::query_scalar::<_, i64>("SELECT count(*) FROM linch_tool_cache");
        count.fetch_one(&db.pool).await.unwrap()
    };
    assert_eq!(rows().await, 3i64, "expired rows linger until swept");
    assert!(put(&cache, "live", "k", "new", &room(LONG, 8)).await);
    assert_eq!(rows().await, 1i64, "only the live row is left");
    db.finish().await;
}
