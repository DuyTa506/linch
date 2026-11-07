//! The memory journal store and the content keys.

use serde_json::json;

use crate::workflow::conformance::journal_store_suite;
use crate::workflow::*;

#[tokio::test]
async fn the_memory_store_passes_the_conformance_suite() {
    journal_store_suite(&MemoryJournalStore::default()).await;
}

#[test]
fn step_keys_separate_names_inputs_and_domains() {
    let plain = step_key("fetch", None).unwrap();
    assert_eq!(plain.len(), 64);
    assert_eq!(plain, step_key("fetch", None).unwrap());
    assert_ne!(plain, step_key("fetch", Some(&json!("a"))).unwrap());
    assert_ne!(plain, step_key("publish", None).unwrap());
    assert_ne!(plain, interrupt_key("fetch"));
}

#[test]
fn step_keys_ignore_object_key_order_but_not_array_order() {
    let ab = step_key("s", Some(&json!({"a": 1, "b": 2}))).unwrap();
    let ba = step_key("s", Some(&json!({"b": 2, "a": 1}))).unwrap();
    assert_eq!(ab, ba);
    let forward = step_key("s", Some(&json!([1, 2]))).unwrap();
    let backward = step_key("s", Some(&json!([2, 1]))).unwrap();
    assert_ne!(forward, backward);
}
