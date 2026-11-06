//! Model check: `SessionLog` against a naive `Vec<Message>` reference.

use super::*;

/// Rebuilds `(full_history, provider_view)` from scratch by replaying the
/// fold rule the module docs describe over `log.entries()` alone: a message
/// entry appends to each projection it's flagged for, a projection entry
/// replaces the derived view outright. This must always agree with the
/// live, cached projections — a log whose `entries()` journal doesn't
/// actually justify its cached views would still pass every other check in
/// this test, since none of them read `entries()`.
fn replay_entries(log: &SessionLog) -> (Vec<Message>, Vec<Message>) {
    let mut full = Vec::new();
    let mut derived = Vec::new();
    for entry in log.entries() {
        match entry {
            LogEntry::Message(entry) => {
                if entry.historical {
                    full.push((*entry.message).clone());
                }
                if entry.visible {
                    derived.push((*entry.message).clone());
                }
            }
            LogEntry::Projection(entry) => {
                derived = entry.replacement.iter().map(|m| (**m).clone()).collect();
            }
        }
    }
    (full, derived)
}

#[derive(Debug, Clone)]
enum Op {
    Append {
        tag: u32,
        visible: bool,
        historical: bool,
    },
    ProjectFresh {
        tags: Vec<u32>,
        reason: String,
    },
    ProjectSameAsCurrent,
}

fn op_strategy() -> impl Strategy<Value = Op> {
    prop_oneof![
        (0u32..50, prop::bool::ANY, prop::bool::ANY).prop_map(|(tag, visible, historical)| {
            Op::Append {
                tag,
                visible,
                historical,
            }
        }),
        (prop::collection::vec(0u32..50, 0..4), "[a-z]{0,6}")
            .prop_map(|(tags, reason)| { Op::ProjectFresh { tags, reason } }),
        Just(Op::ProjectSameAsCurrent),
    ]
}

proptest! {
    #[test]
    fn model_check_matches_naive_reference(ops in prop::collection::vec(op_strategy(), 0..40)) {
        let mut log = SessionLog::new();
        let mut ref_full: Vec<Message> = Vec::new();
        let mut ref_derived: Vec<Message> = Vec::new();

        for op in ops {
            match op {
                Op::Append { tag, visible, historical } => {
                    let message = make_message(tag);
                    if historical {
                        ref_full.push(message.clone());
                    }
                    if visible {
                        ref_derived.push(message.clone());
                    }
                    log.append(message, visible, historical);
                }
                Op::ProjectFresh { tags, reason } => {
                    let replacement: Vec<Message> = tags.iter().map(|t| make_message(*t)).collect();
                    let expected_changed = replacement != ref_derived;
                    if expected_changed {
                        ref_derived = replacement.clone();
                    }
                    let changed = log.record_projection(arc_messages(&replacement), reason);
                    prop_assert_eq!(changed, expected_changed);
                }
                Op::ProjectSameAsCurrent => {
                    let replacement = log.provider_view();
                    // Equals the current view by construction: never a change.
                    prop_assert!(!log.record_projection((*replacement).clone(), "same"));
                }
            }
            prop_assert_eq!(history_as_messages(&log), ref_full.clone());
            prop_assert_eq!(view_as_messages(&log), ref_derived.clone());
            prop_assert_eq!(log.history_count(), ref_full.len());
            prop_assert_eq!(log.visible_count(), ref_derived.len());

            // `entries()` must itself justify the cached projections above,
            // not just happen to agree by construction.
            let (replayed_full, replayed_derived) = replay_entries(&log);
            prop_assert_eq!(replayed_full, history_as_messages(&log));
            prop_assert_eq!(replayed_derived, view_as_messages(&log));
        }

        // "Equal histories" means equal full_history content however
        // reached: a log reseeded from just the historical messages (none of
        // the visible-only entries/projections above) must fingerprint the
        // same as the original.
        let mut reseeded = SessionLog::seed(ref_full, None);
        prop_assert_eq!(
            log.history_fingerprint().unwrap(),
            reseeded.history_fingerprint().unwrap()
        );
    }
}
