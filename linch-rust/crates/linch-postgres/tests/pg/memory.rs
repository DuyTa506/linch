//! The Postgres memory store: the conformance suite, tenants and scopes
//! kept apart, hostile queries, a cap that concurrent saves cannot pass, and
//! text in more than one language.

use linch_core::memory::conformance::{memory_cap_suite, memory_store_suite};
use linch_core::{MemoryError, MemoryStore, NewMemory};
use linch_postgres::PgMemoryStore;
use tokio::task::JoinSet;

use crate::common::TestDb;

async fn save(store: &PgMemoryStore, scope: Option<&str>, key: &str, text: &str) -> String {
    let memory = NewMemory::new(key, text, &[]).unwrap();
    store.save(scope, memory).await.unwrap().id
}

async fn texts(store: &PgMemoryStore, scope: Option<&str>, query: &str) -> Vec<String> {
    let found = store.search(scope, query, 20).await.unwrap();
    found.into_iter().map(|memory| memory.content).collect()
}

#[tokio::test]
async fn the_store_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("memory_suite").await else {
        return;
    };
    memory_store_suite(&PgMemoryStore::new(db.pool.clone(), "ns")).await;
    db.finish().await;
}

#[tokio::test]
async fn a_capped_store_passes_the_cap_suite() {
    let Some(db) = TestDb::migrated("memory_cap_suite").await else {
        return;
    };
    memory_cap_suite(&PgMemoryStore::new(db.pool.clone(), "ns").with_cap(3), 3).await;
    db.finish().await;
}

/// Two namespaces are two memories, whatever the scope, and an id of one
/// is not found from the other.
#[tokio::test]
async fn namespaces_keep_their_memories_apart() {
    let Some(db) = TestDb::migrated("memory_tenants").await else {
        return;
    };
    let a = PgMemoryStore::new(db.pool.clone(), "tenant-a");
    let b = PgMemoryStore::new(db.pool.clone(), "tenant-b");
    let id = save(&a, None, "k", "sharedword secret of a").await;
    save(&a, Some("1:t:ann"), "k2", "sharedword ann of a").await;
    save(&b, None, "k", "sharedword note of b").await;
    assert_eq!(
        texts(&a, None, "sharedword").await,
        ["sharedword secret of a"]
    );
    assert_eq!(
        texts(&b, None, "sharedword").await,
        ["sharedword note of b"]
    );
    assert!(texts(&b, Some("1:t:ann"), "sharedword").await.is_empty());
    assert!(
        !b.delete(None, &id).await.unwrap(),
        "b cannot delete a's row"
    );
    assert_eq!(texts(&a, None, "secret").await.len(), 1);
    assert!(a.delete(None, &id).await.unwrap());
    db.finish().await;
}

/// A hostile query is words and nothing else: it never errors, never
/// matches what it did not name, and leaves the table alone.
#[tokio::test]
async fn hostile_queries_are_only_words() {
    let Some(db) = TestDb::migrated("memory_hostile").await else {
        return;
    };
    let store = PgMemoryStore::new(db.pool.clone(), "ns");
    save(&store, None, "a", "alpha beta").await;
    save(&store, Some("1:t:ann"), "b", "private gamma").await;
    for query in [
        "') OR 1=1 --",
        "'; DROP TABLE linch_memories; --",
        "alpha & | ! ( ) :* <-> beta'",
        "\\' \" \u{0} ((((",
        "!!!alpha",
        "alpha:* & !beta | ",
        "$1 $2 %s {} ${x}",
        "\u{1F600} \u{202E}",
    ] {
        let found = store.search(None, query, 20).await;
        assert!(found.is_ok(), "query {query:?}: {found:?}");
        let found = found.unwrap();
        assert!(
            found.iter().all(|m| m.content != "private gamma"),
            "query {query:?} reached another scope"
        );
    }
    assert!(texts(&store, None, "') OR 1=1 --").await.is_empty());
    assert_eq!(texts(&store, None, "alpha:* & !beta").await.len(), 1);
    let table: Option<String> = sqlx::query_scalar("SELECT to_regclass('linch_memories')::text")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert!(table.is_some(), "the table survived");
    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_memories")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(rows, 2);
    db.finish().await;
}

/// Hostile content is stored as text: quotes and SQL in it come back
/// unchanged.
#[tokio::test]
async fn hostile_content_is_stored_as_text() {
    let Some(db) = TestDb::migrated("memory_content").await else {
        return;
    };
    let store = PgMemoryStore::new(db.pool.clone(), "ns");
    let text = "x'); DROP TABLE linch_memories; -- \\ $1 :* & | !";
    save(&store, None, "k", text).await;
    assert_eq!(texts(&store, None, "drop table").await, [text]);
    db.finish().await;
}

/// Vietnamese and English match the same way: no stemming, no stop words.
#[tokio::test]
async fn text_in_any_language_is_found() {
    let Some(db) = TestDb::migrated("memory_languages").await else {
        return;
    };
    let store = PgMemoryStore::new(db.pool.clone(), "ns");
    save(&store, None, "vi", "Người dùng thích cà phê sữa đá").await;
    save(&store, None, "en", "The user likes the running club").await;
    assert_eq!(texts(&store, None, "cà phê").await.len(), 1);
    assert_eq!(texts(&store, None, "THE").await.len(), 1, "no stop words");
    assert!(texts(&store, None, "run").await.is_empty(), "no stemming");
    db.finish().await;
}

/// The cap counts the root's namespaces together, not each alone.
#[tokio::test]
async fn the_cap_counts_the_whole_root() {
    let Some(db) = TestDb::migrated("memory_root_cap").await else {
        return;
    };
    let a = PgMemoryStore::new(db.pool.clone(), "acme/a").with_cap(2);
    let b = PgMemoryStore::new(db.pool.clone(), "acme/b").with_cap(2);
    let other = PgMemoryStore::new(db.pool.clone(), "other/a").with_cap(2);
    save(&a, None, "1", "one").await;
    save(&b, None, "2", "two").await;
    let third = NewMemory::new("3", "three", &[]).unwrap();
    assert!(matches!(
        a.save(None, third).await,
        Err(MemoryError::LimitReached { max: 2 })
    ));
    save(&other, None, "1", "another tenant is not counted").await;
    db.finish().await;
}

/// Saves racing at the cap store exactly `max`.
#[tokio::test]
async fn concurrent_saves_cannot_pass_the_cap() {
    let Some(db) = TestDb::migrated("memory_cap_race").await else {
        return;
    };
    let mut saves = JoinSet::new();
    for n in 0..12 {
        let store = PgMemoryStore::new(db.pool.clone(), format!("acme/{}", n % 3)).with_cap(5);
        saves.spawn(async move {
            let memory = NewMemory::new(format!("k{n}"), &format!("word {n}"), &[]).unwrap();
            store.save(None, memory).await.is_ok()
        });
    }
    let mut stored = 0;
    while let Some(done) = saves.join_next().await {
        stored += usize::from(done.unwrap());
    }
    assert_eq!(stored, 5);
    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_memories")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(rows, 5);
    db.finish().await;
}

/// A save replayed at once from two tasks stores one row.
#[tokio::test]
async fn racing_replays_store_one_row() {
    let Some(db) = TestDb::migrated("memory_replay_race").await else {
        return;
    };
    let mut saves = JoinSet::new();
    for _ in 0..6 {
        let store = PgMemoryStore::new(db.pool.clone(), "ns");
        saves.spawn(async move {
            let memory = NewMemory::new("same", "replayed", &[]).unwrap();
            store.save(None, memory).await.unwrap().id
        });
    }
    let mut ids = Vec::new();
    while let Some(done) = saves.join_next().await {
        ids.push(done.unwrap());
    }
    ids.dedup();
    assert_eq!(ids.len(), 1, "every replay returns the first row");
    db.finish().await;
}

/// A store whose database is gone reports a backend error, and what the
/// error says is the host's to hide.
#[tokio::test]
async fn a_closed_pool_is_a_backend_error() {
    let Some(db) = TestDb::migrated("memory_closed").await else {
        return;
    };
    let store = PgMemoryStore::new(db.pool.clone(), "ns");
    db.pool.close().await;
    let memory = NewMemory::new("k", "text", &[]).unwrap();
    assert!(matches!(
        store.save(None, memory).await,
        Err(MemoryError::Backend(_))
    ));
    assert!(matches!(
        store.search(None, "text", 5).await,
        Err(MemoryError::Backend(_))
    ));
    db.finish().await;
}
