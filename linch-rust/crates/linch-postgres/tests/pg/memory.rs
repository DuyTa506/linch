//! The Postgres memory store: the conformance suite, tenants and scopes
//! kept apart, hostile queries, a cap that concurrent saves cannot pass, and
//! text in more than one language.

use std::time::Duration;

use linch_core::memory::conformance::{memory_cap_suite, memory_store_suite};
use linch_core::{MemoryError, MemoryStore, NewMemory};
use linch_postgres::PgMemoryStore;
use tokio::task::JoinSet;
use unicode_normalization::UnicodeNormalization;

use crate::common::TestDb;

async fn save(store: &PgMemoryStore, scope: Option<&str>, key: &str, text: &str) -> String {
    let memory = NewMemory::new(key, text, &[]).unwrap();
    store.save(scope, memory).await.unwrap().id
}

async fn texts(store: &PgMemoryStore, scope: Option<&str>, query: &str) -> Vec<String> {
    let found = store.search(scope, query, 20, 1).await.unwrap();
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
    memory_cap_suite(
        &PgMemoryStore::new(db.pool.clone(), "ns")
            .with_cap(5)
            .with_scope_cap(2),
        5,
        2,
    )
    .await;
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
        let found = store.search(None, query, 20, 1).await;
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
    let a = PgMemoryStore::new(db.pool.clone(), "acme/a")
        .with_cap(2)
        .with_scope_cap(5);
    let b = PgMemoryStore::new(db.pool.clone(), "acme/b")
        .with_cap(2)
        .with_scope_cap(5);
    let other = PgMemoryStore::new(db.pool.clone(), "other/a")
        .with_cap(2)
        .with_scope_cap(5);
    save(&a, None, "1", "one").await;
    save(&b, None, "2", "two").await;
    let third = NewMemory::new("3", "three", &[]).unwrap();
    assert!(matches!(
        a.save(None, third).await,
        Err(MemoryError::LimitReached {
            max: 2,
            per_scope: false
        })
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
        let store = PgMemoryStore::new(db.pool.clone(), format!("acme/{}", n % 3))
            .with_cap(5)
            .with_scope_cap(5);
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
        store.search(None, "text", 5, 1).await,
        Err(MemoryError::Backend(_))
    ));
    db.finish().await;
}

/// A save that waits on the cap's lock ends at the statement timeout with
/// a backend error, and a held lock does not hold the connection forever.
#[tokio::test]
async fn a_save_waiting_on_the_cap_lock_times_out() {
    let Some(db) = TestDb::migrated("memory_lock_timeout").await else {
        return;
    };
    let mut holder = db.admin.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('linch_memory_cap:acme', 0))")
        .execute(&mut *holder)
        .await
        .unwrap();
    let store = PgMemoryStore::new(db.pool.clone(), "acme/a")
        .with_cap(8)
        .with_statement_timeout(Duration::from_millis(300));
    let memory = NewMemory::new("k", "waits", &[]).unwrap();
    let saved = tokio::time::timeout(Duration::from_secs(10), store.save(None, memory)).await;
    assert!(
        matches!(saved, Ok(Err(MemoryError::Backend(_)))),
        "the wait ends in a backend error: {saved:?}"
    );
    holder.rollback().await.unwrap();
    let memory = NewMemory::new("k", "waits", &[]).unwrap();
    store.save(None, memory).await.expect("saves once free");
    db.finish().await;
}

/// The timeout is a transaction-local setting: it does not leak into the
/// pooled connection the next caller gets.
#[tokio::test]
async fn the_statement_timeout_does_not_outlive_the_call() {
    let Some(db) = TestDb::migrated("memory_timeout_local").await else {
        return;
    };
    let one = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect_with(db.options.clone())
        .await
        .unwrap();
    let before: String = sqlx::query_scalar("SHOW statement_timeout")
        .fetch_one(&one)
        .await
        .unwrap();
    let store =
        PgMemoryStore::new(one.clone(), "ns").with_statement_timeout(Duration::from_millis(321));
    save(&store, None, "k", "something").await;
    texts(&store, None, "something").await;
    let after: String = sqlx::query_scalar("SHOW statement_timeout")
        .fetch_one(&one)
        .await
        .unwrap();
    assert_eq!(before, after);
    one.close().await;
    db.finish().await;
}

/// A scope fills at its own cap, naming it, and leaves room for others.
#[tokio::test]
async fn one_scope_cannot_fill_the_tenants_budget() {
    let Some(db) = TestDb::migrated("memory_scope_cap").await else {
        return;
    };
    // 12 for the tenant, a quarter (3) for each scope.
    let store = PgMemoryStore::new(db.pool.clone(), "acme/a").with_cap(12);
    for n in 0..3 {
        save(&store, Some("1:t:ann"), &format!("a{n}"), "ann fact").await;
    }
    let more = NewMemory::new("a3", "ann again", &[]).unwrap();
    assert!(matches!(
        store.save(Some("1:t:ann"), more).await,
        Err(MemoryError::LimitReached {
            max: 3,
            per_scope: true
        })
    ));
    // Another namespace of the tenant counts the same scope.
    let sibling = PgMemoryStore::new(db.pool.clone(), "acme/b").with_cap(12);
    let more = NewMemory::new("b0", "ann elsewhere", &[]).unwrap();
    assert!(matches!(
        sibling.save(Some("1:t:ann"), more).await,
        Err(MemoryError::LimitReached {
            per_scope: true,
            ..
        })
    ));
    save(&store, Some("1:t:bob"), "b0", "bob fact").await;
    save(&store, None, "s0", "shared fact").await;
    db.finish().await;
}

/// A purge reaches its own namespace and scope only.
#[tokio::test]
async fn a_purge_leaves_other_namespaces_alone() {
    let Some(db) = TestDb::migrated("memory_purge").await else {
        return;
    };
    let a = PgMemoryStore::new(db.pool.clone(), "tenant-a");
    let b = PgMemoryStore::new(db.pool.clone(), "tenant-b");
    save(&a, Some("1:t:ann"), "1", "purgeword of a").await;
    save(&b, Some("1:t:ann"), "1", "purgeword of b").await;
    assert_eq!(a.purge(Some("1:t:ann")).await.unwrap(), 1);
    assert!(texts(&a, Some("1:t:ann"), "purgeword").await.is_empty());
    assert_eq!(texts(&b, Some("1:t:ann"), "purgeword").await.len(), 1);
    db.finish().await;
}

/// A recall query of two Vietnamese words finds the text, a decomposed
/// spelling included, and a row with one of the words alone does not match.
#[tokio::test]
async fn vietnamese_recall_needs_two_words_and_reads_either_spelling() {
    let Some(db) = TestDb::migrated("memory_vietnamese").await else {
        return;
    };
    let store = PgMemoryStore::new(db.pool.clone(), "ns");
    save(&store, None, "1", "Người dùng thích uống cà phê sữa đá").await;
    save(&store, None, "2", "Người dùng sống ở Hà Nội").await;
    let both = store.search(None, "thích cà phê", 5, 2).await.unwrap();
    assert_eq!(both.len(), 1);
    let shared = store.search(None, "người dùng", 5, 2).await.unwrap();
    assert_eq!(shared.len(), 2, "both words are in both rows");
    let split = store.search(None, "thích hà", 5, 2).await.unwrap();
    assert!(split.is_empty(), "no row has both");
    let decomposed: String = "cà phê".nfd().collect();
    assert_eq!(
        store.search(None, &decomposed, 5, 2).await.unwrap().len(),
        1
    );
    db.finish().await;
}
