use std::sync::Arc;

use proptest::prelude::*;

use super::*;
use crate::message::Message;

fn make_message(tag: u32) -> Message {
    if tag.is_multiple_of(2) {
        Message::user_text(format!("u{tag}"))
    } else {
        Message::assistant_text(format!("a{tag}"))
    }
}

fn arc_messages(messages: &[Message]) -> Vec<Arc<Message>> {
    messages.iter().cloned().map(Arc::new).collect()
}

fn history_as_messages(log: &SessionLog) -> Vec<Message> {
    log.full_history().iter().map(|m| (**m).clone()).collect()
}

fn view_as_messages(log: &SessionLog) -> Vec<Message> {
    log.provider_view().iter().map(|m| (**m).clone()).collect()
}

#[test]
fn append_defaults_place_message_in_both_projections() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    assert_eq!(log.history_count(), 1);
    assert_eq!(log.visible_count(), 1);
    assert_eq!(history_as_messages(&log), view_as_messages(&log));
}

#[test]
fn visible_only_message_excluded_from_full_history() {
    let mut log = SessionLog::new();
    log.append(make_message(1), true, false);
    assert_eq!(log.history_count(), 0);
    assert_eq!(log.visible_count(), 1);
    assert!(log.full_history().is_empty());
}

#[test]
fn historical_only_message_excluded_from_provider_view() {
    let mut log = SessionLog::new();
    log.append(make_message(1), false, true);
    assert_eq!(log.history_count(), 1);
    assert_eq!(log.visible_count(), 0);
    assert!(log.provider_view().is_empty());
}

#[test]
fn neither_flag_logs_an_entry_in_no_projection() {
    let mut log = SessionLog::new();
    log.append(make_message(0), false, false);
    assert_eq!(log.entries().len(), 1);
    assert_eq!(log.history_count(), 0);
    assert_eq!(log.visible_count(), 0);
}

#[test]
fn record_projection_no_op_when_replacement_equals_current_view() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    let current = (*log.provider_view()).clone();
    let changed = log.record_projection(current, "no-op");
    assert!(!changed);
    assert_eq!(log.entries().len(), 1, "a no-op projection is not logged");
}

#[test]
fn record_projection_changes_view_and_is_logged() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    log.append(make_message(1), true, true);
    let replacement = arc_messages(&[Message::assistant_text("summary")]);
    let changed = log.record_projection(replacement.clone(), "compaction");
    assert!(changed);
    assert_eq!(log.entries().len(), 3);
    assert_eq!(log.visible_count(), 1);
    assert_eq!(
        view_as_messages(&log),
        vec![Message::assistant_text("summary")]
    );
    match log.entries().last().unwrap() {
        LogEntry::Projection(entry) => {
            assert_eq!(entry.reason, "compaction");
            assert_eq!(entry.replacement, replacement);
        }
        other => panic!("expected a projection entry, got {other:?}"),
    }
}

#[test]
fn full_history_is_unaffected_by_projections() {
    let mut log = SessionLog::new();
    for tag in 0..5 {
        log.append(make_message(tag), true, true);
    }
    let before = history_as_messages(&log);
    log.record_projection(arc_messages(&[Message::assistant_text("s1")]), "c1");
    log.record_projection(Vec::new(), "c2");
    log.record_projection(arc_messages(&[Message::user_text("s2")]), "c3");
    assert_eq!(history_as_messages(&log), before);
    assert_eq!(log.history_count(), 5);
}

#[test]
fn entries_only_grow() {
    let mut log = SessionLog::new();
    let mut previous_len = log.entries().len();
    for tag in 0..4 {
        log.append(make_message(tag), tag.is_multiple_of(2), true);
        assert!(log.entries().len() > previous_len);
        previous_len = log.entries().len();
    }
    log.record_projection(Vec::new(), "");
    assert!(log.entries().len() > previous_len);
}

#[test]
fn last_visible_returns_newest_provider_view_message_or_none() {
    let mut log = SessionLog::new();
    assert!(log.last_visible().is_none());
    log.append(make_message(0), true, true);
    log.append(make_message(1), true, true);
    assert_eq!(log.last_visible(), Some(&make_message(1)));
    log.append(make_message(2), false, true);
    assert_eq!(
        log.last_visible(),
        Some(&make_message(1)),
        "a historical-only append must not change the newest visible message"
    );
}

#[test]
fn seed_without_visible_mirrors_historical_as_provider_view() {
    let historical = vec![make_message(0), make_message(1)];
    let log = SessionLog::seed(historical.clone(), None);
    assert_eq!(history_as_messages(&log), historical);
    assert_eq!(view_as_messages(&log), historical);
    assert_eq!(
        log.entries().len(),
        2,
        "seeding records no projection entry"
    );
}

#[test]
fn seed_with_differing_visible_records_one_projection() {
    let historical = vec![make_message(0), make_message(1), make_message(2)];
    let visible = vec![Message::assistant_text("compacted")];
    let log = SessionLog::seed(historical.clone(), Some(visible.clone()));
    assert_eq!(history_as_messages(&log), historical);
    assert_eq!(view_as_messages(&log), visible);
    assert_eq!(
        log.entries().len(),
        4,
        "3 historical appends + 1 projection"
    );
}

#[test]
fn seed_with_visible_equal_to_historical_records_no_projection() {
    let historical = vec![make_message(0), make_message(1)];
    let log = SessionLog::seed(historical.clone(), Some(historical.clone()));
    assert_eq!(
        log.entries().len(),
        2,
        "an unchanged view seeds no projection"
    );
}

#[test]
fn snapshot_isolation_under_copy_on_write() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    let snapshot = log.provider_view();
    assert_eq!(snapshot.len(), 1);

    log.append(make_message(1), true, true);
    assert_eq!(
        snapshot.len(),
        1,
        "a held snapshot must not observe a later write"
    );
    assert_eq!(log.provider_view().len(), 2);

    let full_snapshot = log.full_history();
    log.record_projection(Vec::new(), "drop everything");
    assert_eq!(
        full_snapshot.len(),
        2,
        "a projection must never affect a full_history snapshot"
    );
}

#[test]
fn unchanged_reads_share_the_same_arc() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    let first = log.provider_view();
    let second = log.provider_view();
    assert!(
        Arc::ptr_eq(&first, &second),
        "two reads with no write between them must share the cached Arc"
    );
    log.append(make_message(1), true, true);
    let third = log.provider_view();
    assert!(
        !Arc::ptr_eq(&first, &third),
        "a write must produce a new Arc"
    );
}

#[test]
fn history_fingerprint_matches_pinned_vectors() {
    // Computed with Python hashlib over the canonical JSON array of messages.
    let mut log = SessionLog::new();
    assert_eq!(
        log.history_fingerprint().to_string(),
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    );
    log.append(Message::user_text("hi"), true, true);
    assert_eq!(
        log.history_fingerprint().to_string(),
        "4047097b2648d6b400be8a670f467110f9366f7b79e3b450d3eb138e36acdab4"
    );
}

#[test]
fn history_fingerprint_is_the_digest_of_the_history_array() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    log.append(make_message(1), true, false);
    log.append(make_message(2), false, true);
    let history = serde_json::to_value(history_as_messages(&log)).unwrap();
    assert_eq!(
        log.history_fingerprint(),
        crate::canonical::digest(&history)
    );
}

#[test]
fn history_fingerprint_is_stable_without_new_appends() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    let first = log.history_fingerprint();
    let second = log.history_fingerprint();
    assert_eq!(first, second);
}

#[test]
fn history_fingerprint_changes_only_on_historical_append() {
    let mut log = SessionLog::new();
    log.append(make_message(0), true, true);
    let before = log.history_fingerprint();

    log.append(make_message(1), true, false); // visible-only
    assert_eq!(
        log.history_fingerprint(),
        before,
        "visible-only append must not move the digest"
    );

    log.record_projection(Vec::new(), "compact");
    assert_eq!(
        log.history_fingerprint(),
        before,
        "a projection must not move the digest"
    );

    log.append(make_message(2), false, true); // historical
    assert_ne!(
        log.history_fingerprint(),
        before,
        "a historical append must move the digest"
    );
}

#[test]
fn history_fingerprint_is_insensitive_to_metadata_key_order() {
    let mut first = Message::user_text("hi");
    first.provider_metadata = Some(
        serde_json::json!({"a": 1, "b": 2})
            .as_object()
            .unwrap()
            .clone(),
    );
    let mut second = Message::user_text("hi");
    second.provider_metadata = Some({
        let mut map = serde_json::Map::new();
        map.insert("b".to_string(), serde_json::json!(2));
        map.insert("a".to_string(), serde_json::json!(1));
        map
    });
    assert_ne!(
        serde_json::to_string(&first.provider_metadata).unwrap(),
        serde_json::to_string(&second.provider_metadata).unwrap(),
        "the two maps must actually differ in insertion order for this test to be meaningful"
    );

    let mut log_a = SessionLog::new();
    log_a.append(first, true, true);
    let mut log_b = SessionLog::new();
    log_b.append(second, true, true);
    assert_eq!(log_a.history_fingerprint(), log_b.history_fingerprint());
}

proptest! {
    #[test]
    fn history_fingerprint_is_independent_of_query_cadence(tags in prop::collection::vec((0u32..50, prop::bool::ANY, prop::bool::ANY), 0..25)) {
        let mut log_queried_every_step = SessionLog::new();
        for (tag, visible, historical) in &tags {
            log_queried_every_step.append(make_message(*tag), *visible, *historical);
            let _ = log_queried_every_step.history_fingerprint();
        }
        let final_from_every_step = log_queried_every_step.history_fingerprint();

        let mut log_queried_once = SessionLog::new();
        for (tag, visible, historical) in &tags {
            log_queried_once.append(make_message(*tag), *visible, *historical);
        }
        let final_from_once = log_queried_once.history_fingerprint();

        prop_assert_eq!(final_from_every_step, final_from_once);
    }
}

mod model;
