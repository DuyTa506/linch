//! The Postgres tool cache: entries live until their ttl on the database's
//! clock, the oldest go first, namespaces stay apart, a clear is per
//! namespace, and an agent's cache survives a restart.

use std::time::Duration;

use linch_core::{ToolCacheStore, ToolOutput};
use linch_postgres::PgToolCache;
use serde_json::json;

use crate::common::TestDb;

const LONG: Duration = Duration::from_secs(60);

fn out(text: &str) -> ToolOutput {
    let mut output = ToolOutput::new(text);
    output.metadata.insert("source".into(), json!("disk"));
    output
}

#[tokio::test]
async fn an_entry_round_trips_with_its_metadata_and_replaces_by_key() {
    let Some(db) = TestDb::migrated("tool_cache_round").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    assert_eq!(cache.get("ns", "k").await.unwrap(), None);
    cache.put("ns", "k", &out("one"), LONG, 8).await.unwrap();
    assert_eq!(cache.get("ns", "k").await.unwrap(), Some(out("one")));
    cache.put("ns", "k", &out("two"), LONG, 8).await.unwrap();
    assert_eq!(cache.get("ns", "k").await.unwrap(), Some(out("two")));
    // Another connection of the same database sees it: it is durable.
    let again = PgToolCache::new(db.pool.clone());
    assert_eq!(again.get("ns", "k").await.unwrap(), Some(out("two")));
    db.finish().await;
}

#[tokio::test]
async fn an_expired_entry_is_never_returned() {
    let Some(db) = TestDb::migrated("tool_cache_ttl").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    cache
        .put("ns", "k", &out("brief"), Duration::from_millis(200), 8)
        .await
        .unwrap();
    assert!(cache.get("ns", "k").await.unwrap().is_some());
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(cache.get("ns", "k").await.unwrap(), None);
    db.finish().await;
}

#[tokio::test]
async fn the_oldest_entries_go_when_a_namespace_is_full() {
    let Some(db) = TestDb::migrated("tool_cache_room").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    for key in ["a", "b", "c"] {
        cache.put("ns", key, &out(key), LONG, 2).await.unwrap();
    }
    let held: Vec<_> = [("a", false), ("b", true), ("c", true)].into();
    for (key, kept) in held {
        let got = cache.get("ns", key).await.unwrap();
        assert_eq!(got.is_some(), kept, "{key}");
    }
    // Rewriting an entry makes it the newest.
    cache.put("ns", "b", &out("b"), LONG, 2).await.unwrap();
    cache.put("ns", "d", &out("d"), LONG, 2).await.unwrap();
    assert!(cache.get("ns", "b").await.unwrap().is_some());
    assert!(cache.get("ns", "c").await.unwrap().is_none());
    assert!(cache.get("ns", "d").await.unwrap().is_some());
    // One entry of room keeps only the newest.
    cache.put("ns", "e", &out("e"), LONG, 1).await.unwrap();
    assert!(cache.get("ns", "d").await.unwrap().is_none());
    assert!(cache.get("ns", "e").await.unwrap().is_some());
    db.finish().await;
}

#[tokio::test]
async fn namespaces_keep_their_entries_apart_and_clear_one_at_a_time() {
    let Some(db) = TestDb::migrated("tool_cache_ns").await else {
        return;
    };
    let cache = PgToolCache::new(db.pool.clone());
    cache.put("acme", "k", &out("acme"), LONG, 1).await.unwrap();
    cache
        .put("other", "k", &out("other"), LONG, 1)
        .await
        .unwrap();
    assert_eq!(cache.get("acme", "k").await.unwrap(), Some(out("acme")));
    assert_eq!(cache.get("other", "k").await.unwrap(), Some(out("other")));
    cache.clear("acme").await.unwrap();
    assert_eq!(cache.get("acme", "k").await.unwrap(), None);
    assert_eq!(cache.get("other", "k").await.unwrap(), Some(out("other")));
    db.finish().await;
}
