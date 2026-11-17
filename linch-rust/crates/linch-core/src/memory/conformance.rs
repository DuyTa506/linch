//! Memory store conformance suite shared by every backend.
//!
//! Every [`MemoryStore`] must pass [`memory_store_suite`], and a store built
//! with a cap must pass [`memory_cap_suite`]. A failed rule panics with a
//! message naming it, so call the suite from a test. Run each on a store
//! that holds nothing.
//!
//! The rules:
//! - a saved memory keeps its fields, with tags normalized;
//! - saving a key again returns the first row and stores nothing;
//! - a search matches any word of the query in content or tags, ranks more
//!   matches first, honours the limit, and finds nothing for a query that
//!   has no words (operators, quotes and SQL are only punctuation);
//! - scopes are apart: the shared scope and each principal's never see one
//!   another's rows, and a delete reaches only its own scope;
//! - a delete of an unknown id, whatever it looks like, finds nothing;
//! - a full cap refuses a new memory, not a replay, and a delete frees room.

use super::{Memory, MemoryError, MemoryStore, NewMemory};

async fn save(store: &dyn MemoryStore, scope: Option<&str>, key: &str, text: &str) -> Memory {
    let memory = NewMemory::new(key, text, &[]).expect("a valid memory");
    store.save(scope, memory).await.expect("save")
}

async fn found(store: &dyn MemoryStore, scope: Option<&str>, query: &str) -> Vec<String> {
    let found = store.search(scope, query, 20).await.expect("search");
    found.into_iter().map(|memory| memory.content).collect()
}

/// Check every [`MemoryStore`] rule. Panics on the first rule that fails.
pub async fn memory_store_suite(store: &dyn MemoryStore) {
    a_memory_keeps_its_fields(store).await;
    a_key_saves_once(store).await;
    a_search_matches_any_word_best_first(store).await;
    a_search_reads_tags(store).await;
    a_search_honours_its_limit(store).await;
    a_query_without_words_finds_nothing(store).await;
    scopes_are_apart(store).await;
    a_delete_reaches_its_own_scope_only(store).await;
    a_delete_of_a_strange_id_finds_nothing(store).await;
}

async fn a_memory_keeps_its_fields(store: &dyn MemoryStore) {
    let tags = ["Pets".to_owned(), "pets".to_owned()];
    let new = NewMemory::new("fields", " the cat is called Miso ", &tags).unwrap();
    let saved = store.save(None, new).await.expect("save");
    assert_eq!(saved.content, "the cat is called Miso", "content kept");
    assert_eq!(saved.tags, ["pets"], "tags normalized");
    assert!(!saved.id.is_empty(), "an id is assigned");
    assert!(saved.created_at_ms > 0, "a creation time is set");
}

async fn a_key_saves_once(store: &dyn MemoryStore) {
    let first = save(store, None, "replay", "replayword stored once").await;
    let again = save(store, None, "replay", "replayword stored once").await;
    assert_eq!(first, again, "a replayed key returns the first row");
    let hits = found(store, None, "replayword").await;
    assert_eq!(hits.len(), 1, "a replayed key stores one row");
}

async fn a_search_matches_any_word_best_first(store: &dyn MemoryStore) {
    save(store, None, "rank-1", "ranka alone here").await;
    save(store, None, "rank-2", "ranka and rankb together").await;
    save(store, None, "rank-3", "unrelated words").await;
    let hits = found(store, None, "RANKA, rankb?").await;
    assert_eq!(
        hits,
        ["ranka and rankb together", "ranka alone here"],
        "any word matches, more matches first"
    );
}

async fn a_search_reads_tags(store: &dyn MemoryStore) {
    let tags = ["zebrafish".to_owned()];
    let new = NewMemory::new("tagged", "an aquarium note", &tags).unwrap();
    store.save(None, new).await.expect("save");
    let hits = found(store, None, "zebrafish").await;
    assert_eq!(hits, ["an aquarium note"], "a tag is searchable");
}

async fn a_search_honours_its_limit(store: &dyn MemoryStore) {
    for n in 0..4 {
        save(
            store,
            None,
            &format!("limit-{n}"),
            &format!("limitword {n}"),
        )
        .await;
    }
    let hits = store.search(None, "limitword", 2).await.expect("search");
    assert_eq!(hits.len(), 2, "the limit caps the results");
    let none = store.search(None, "limitword", 0).await.expect("search");
    assert!(none.is_empty(), "a limit of 0 returns nothing");
}

async fn a_query_without_words_finds_nothing(store: &dyn MemoryStore) {
    save(store, None, "plain", "plain stored text").await;
    for query in [
        "",
        "   ",
        "') OR 1=1 --",
        "& | ! ( ) :* <->",
        "\\ \" ' ; DROP TABLE linch_memories",
        "plain & ( | !",
    ] {
        let hits = store.search(None, query, 20).await;
        assert!(hits.is_ok(), "query {query:?} must not fail the store");
    }
    for query in ["", "   ", "& | ! ( ) :* <->", "' \" ; \\"] {
        assert!(
            found(store, None, query).await.is_empty(),
            "query {query:?} has no words, so it matches nothing"
        );
    }
    let hits = found(store, None, "') OR 1=1 --").await;
    assert!(
        !hits.iter().any(|text| text == "plain stored text"),
        "an injected OR matches nothing it did not name"
    );
}

async fn scopes_are_apart(store: &dyn MemoryStore) {
    save(store, None, "shared", "scopeword shared note").await;
    save(store, Some("1:t:ann"), "ann", "scopeword ann note").await;
    save(store, Some("1:t:bob"), "bob", "scopeword bob note").await;
    assert_eq!(
        found(store, None, "scopeword").await,
        ["scopeword shared note"]
    );
    assert_eq!(
        found(store, Some("1:t:ann"), "scopeword").await,
        ["scopeword ann note"]
    );
    assert_eq!(
        found(store, Some("1:t:bob"), "scopeword").await,
        ["scopeword bob note"]
    );
    assert!(
        found(store, Some("1:t:cy"), "scopeword").await.is_empty(),
        "a scope with no rows finds none"
    );
}

async fn a_delete_reaches_its_own_scope_only(store: &dyn MemoryStore) {
    let ann = save(store, Some("1:t:dan"), "dan", "deleteword dan note").await;
    assert!(
        !store.delete(None, &ann.id).await.unwrap(),
        "the shared scope cannot delete a principal's memory"
    );
    assert!(
        !store.delete(Some("1:t:eve"), &ann.id).await.unwrap(),
        "another principal cannot delete it"
    );
    assert_eq!(
        found(store, Some("1:t:dan"), "deleteword").await.len(),
        1,
        "a refused delete leaves the row"
    );
    assert!(store.delete(Some("1:t:dan"), &ann.id).await.unwrap());
    assert!(
        !store.delete(Some("1:t:dan"), &ann.id).await.unwrap(),
        "a second delete finds nothing"
    );
    assert!(found(store, Some("1:t:dan"), "deleteword").await.is_empty());
    let shared = save(store, None, "shared-del", "deleteword shared note").await;
    assert!(
        !store.delete(Some("1:t:dan"), &shared.id).await.unwrap(),
        "a principal cannot delete a shared memory"
    );
    assert!(store.delete(None, &shared.id).await.unwrap());
}

async fn a_delete_of_a_strange_id_finds_nothing(store: &dyn MemoryStore) {
    for id in [
        "",
        "nope",
        "' OR '1'='1",
        "%",
        "../x",
        "0",
        &"x".repeat(5000),
    ] {
        assert!(!store.delete(None, id).await.unwrap(), "id {id:?}");
    }
}

/// Check a store built with a cap of `max` memories, holding nothing.
/// Panics on the first rule that fails.
pub async fn memory_cap_suite(store: &dyn MemoryStore, max: u64) {
    let mut saved = Vec::new();
    for n in 0..max {
        saved.push(save(store, None, &format!("cap-{n}"), &format!("capword {n}")).await);
    }
    let over = NewMemory::new("cap-over", "one too many", &[]).unwrap();
    match store.save(None, over).await {
        Err(MemoryError::LimitReached { max: reported }) => assert_eq!(reported, max),
        other => panic!("a save past the cap must be refused, got {other:?}"),
    }
    let other_scope = NewMemory::new("cap-other", "another scope", &[]).unwrap();
    assert!(
        matches!(
            store.save(Some("1:t:ann"), other_scope).await,
            Err(MemoryError::LimitReached { .. })
        ),
        "the cap counts every scope of the namespace"
    );
    let replay = save(store, None, "cap-0", "capword 0").await;
    assert_eq!(
        replay, saved[0],
        "a replay at the cap returns the stored row"
    );
    assert!(store.delete(None, &saved[0].id).await.unwrap());
    save(store, None, "cap-after", "room again").await;
}
