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
    let named = InMemoryMemoryStore::new().with_cap(5).with_scope_cap(2);
    conformance::memory_cap_suite(&named, 5, 2).await;
    // Without a named per-scope cap, a quarter of the cap (8 / 4 = 2).
    conformance::memory_cap_suite(&InMemoryMemoryStore::new().with_cap(8), 8, 2).await;
}

#[test]
fn recall_terms_drop_what_selects_nothing() {
    assert_eq!(
        recall_terms("What is the plan for the deploy of it?"),
        ["plan", "deploy"]
    );
    assert_eq!(recall_terms("a an to of is"), Vec::<String>::new());
    // Digits are kept even when short.
    assert_eq!(recall_terms("port 80 or 443"), ["port", "80", "443"]);
}

#[test]
fn recall_terms_keep_vietnamese_words() {
    // `ăn`, `mẹ` and `đi` carry a diacritic; only the particles go.
    assert_eq!(
        recall_terms("Mẹ tôi thích ăn phở và đi chợ"),
        ["mẹ", "tôi", "thích", "ăn", "phở", "đi", "chợ"]
    );
}

#[test]
fn the_cap_counts_the_words_that_remain() {
    let noise = "the and for are ".repeat(40);
    let words = (0..40).map(|n| format!("word{n}")).collect::<Vec<_>>();
    let terms = recall_terms(&format!("{noise}{}", words.join(" ")));
    assert_eq!(terms.len(), MAX_SEARCH_TERMS);
    assert_eq!(terms[0], "word0");
}

#[test]
fn decomposed_text_is_one_word() {
    use unicode_normalization::UnicodeNormalization;
    let composed = "phở";
    let decomposed: String = composed.nfd().collect();
    assert_ne!(composed, decomposed);
    assert_eq!(search_terms(&decomposed), [composed]);
    assert_eq!(index_words(&format!("x {decomposed}")), ["x", composed]);
}

#[test]
fn a_call_key_names_the_call_and_what_it_saves() {
    let save = |key: &str, text: &str| NewMemory::for_call(key, text, &[]).unwrap().key;
    assert_eq!(save("r:t1", "likes tea"), save("r:t1", "  likes tea "));
    assert_ne!(save("r:t1", "likes tea"), save("r:t1", "likes coffee"));
    assert_ne!(save("r:t1", "likes tea"), save("r:t2", "likes tea"));
    let tagged = NewMemory::for_call("r:t1", "likes tea", &["food".to_owned()]).unwrap();
    assert_ne!(tagged.key, save("r:t1", "likes tea"), "tags are part of it");
    assert!(save("r:t1", "likes tea").starts_with("r:t1:"));
}

#[test]
fn the_scope_cap_defaults_to_a_quarter_and_at_least_one() {
    assert_eq!(default_scope_cap(100), 25);
    assert_eq!(default_scope_cap(3), 1);
    assert_eq!(default_scope_cap(1), 1);
}
