use super::*;

#[test]
fn terms_are_lowercase_words_without_operators() {
    assert_eq!(
        search_terms("Rust AND Postgres, rust!"),
        ["rust", "and", "postgres"]
    );
    assert_eq!(search_terms("') OR 1=1 --"), ["1"]);
    assert!(search_terms("& | ! ( ) :* <-> ' \\ \"").is_empty());
}

#[test]
fn terms_are_bounded() {
    let many = (0..100)
        .map(|n| format!("w{n}"))
        .collect::<Vec<_>>()
        .join(" ");
    assert_eq!(search_terms(&many).len(), MAX_SEARCH_TERMS);
    let long = "x".repeat(500);
    assert_eq!(search_terms(&long)[0].chars().count(), MAX_TERM_CHARS);
}

#[test]
fn a_new_memory_is_normalized_and_checked() {
    let tags = ["  Food ".to_owned(), "food".to_owned(), "Pets".to_owned()];
    let memory = NewMemory::new("k", "  likes tea ", &tags).unwrap();
    assert_eq!(memory.content, "likes tea");
    assert_eq!(memory.tags, ["food", "pets"]);
    assert!(NewMemory::new("k", "   ", &[]).is_err());
    assert!(NewMemory::new("k", "a\0b", &[]).is_err());
    assert!(NewMemory::new("k", "ok", &["a\0".to_owned()]).is_err());
    assert!(NewMemory::new("k", &"x".repeat(MAX_CONTENT_BYTES + 1), &[]).is_err());
    assert!(NewMemory::new("k", "ok", &vec!["t".to_owned(); MAX_TAGS + 1]).is_err());
    assert!(NewMemory::new("k", "ok", &[" ".to_owned()]).is_err());
}

#[test]
fn principal_scopes_never_collide() {
    let a = Principal::new("a/b", "c", ["r"]).unwrap();
    let b = Principal::new("a", "b/c", ["r"]).unwrap();
    assert_ne!(principal_scope(&a), principal_scope(&b));
}

#[tokio::test]
async fn the_in_memory_store_passes_the_conformance_suite() {
    conformance::memory_store_suite(&InMemoryMemoryStore::new()).await;
    conformance::memory_cap_suite(&InMemoryMemoryStore::new().with_cap(2), 2).await;
}
