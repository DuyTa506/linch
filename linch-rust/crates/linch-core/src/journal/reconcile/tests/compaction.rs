//! The reconcile table for the two compaction phases.

use super::*;
use crate::session_log::ProjectionEntry;

/// A compaction delivery: like [`delivery`], of kind `compaction`.
fn summary_delivery(turn: u64, attempt: &str) -> Event {
    let Event::ModelCallDelivery(d) = delivery(turn, attempt) else {
        unreachable!()
    };
    let kind = ModelCallKind::Compaction;
    Event::ModelCallDelivery(ModelCallDeliveryEvent { kind, ..d })
}

fn summary_receipt(attempt: &str, disposition: CallDisposition) -> Event {
    let Event::ModelCallReceipt(r) = receipt(attempt, disposition) else {
        unreachable!()
    };
    let kind = ModelCallKind::Compaction;
    Event::ModelCallReceipt(ModelCallReceiptEvent { kind, ..r })
}

fn projection(summary: &Message) -> LogEntry {
    let first = Arc::new(Message::user_text("m0"));
    LogEntry::Projection(ProjectionEntry {
        replacement: vec![first, Arc::new(summary.clone())],
        reason: "compaction".into(),
    })
}

#[test]
fn a_pending_summary_is_asked_again_and_a_paid_one_is_never_lost() {
    let cp = checkpoint(Phase::CompactionPending { attempt: 1 });
    let log = session(LEN, None);
    let Event::ModelCallDelivery(d) = summary_delivery(2, "c1") else {
        unreachable!()
    };
    let recompact = |abandoned| {
        Ok(Recovery::Recompact {
            attempt: 2,
            abandoned,
        })
    };
    let cases: Vec<(Vec<Event>, Vec<LogEntry>, Outcome)> = vec![
        (
            vec![delivery(2, "p1")],
            log.clone(),
            Err(ReconcileError::MissingDelivery { turn: 2 }),
        ),
        (
            vec![summary_delivery(2, "c1")],
            log.clone(),
            recompact(Some(abandoned(&d))),
        ),
        (
            vec![summary_delivery(2, "c1")],
            session(LEN + 1, None),
            conflict(LEN + 1),
        ),
        (
            vec![
                summary_delivery(2, "c1"),
                summary_receipt("c1", CallDisposition::Failed),
            ],
            log.clone(),
            recompact(None),
        ),
        (
            vec![
                summary_delivery(2, "c1"),
                summary_receipt("c1", CallDisposition::Accepted),
            ],
            log.clone(),
            Err(ReconcileError::MissingSummary {
                delivery_attempt_id: "c1".into(),
            }),
        ),
    ];
    for (events, log, expected) in cases {
        let events = envelopes(events);
        assert_eq!(reconcile(&cp, &events, &log), expected, "{events:#?}");
    }
}

#[test]
fn the_primary_call_ignores_the_summary_call_of_its_turn() {
    let cp = checkpoint(Phase::ProviderPending { attempt: 1 });
    let events = envelopes(vec![
        summary_delivery(2, "c1"),
        summary_receipt("c1", CallDisposition::Accepted),
        delivery(2, "p1"),
    ]);
    let Event::ModelCallDelivery(d) = delivery(2, "p1") else {
        unreachable!()
    };
    let expected = Recovery::Redispatch {
        attempt: 2,
        abandoned: Some(abandoned(&d)),
    };
    let log = session(LEN, None);
    assert_eq!(reconcile(&cp, &events, &log), Ok(expected));
}

#[test]
fn a_received_summary_is_projected_once() {
    let summary = Message::user_text("<summary>");
    let cp = checkpoint(Phase::Compacted {
        summary: summary.clone(),
        keep_from: 5,
    });
    let compacted = |appended| {
        Ok(Recovery::Compacted {
            summary: Message::user_text("<summary>"),
            keep_from: 5,
            appended,
        })
    };
    let mut projected = session(LEN, None);
    projected.push(projection(&summary));
    let mut other = session(LEN, None);
    other.push(projection(&Message::user_text("another")));
    let cases: Vec<(Vec<LogEntry>, Outcome)> = vec![
        (session(LEN, None), compacted(false)),
        (projected, compacted(true)),
        (other, conflict(LEN + 1)),
        (session(LEN + 1, Some(&summary)), conflict(LEN + 1)),
        (session(LEN - 1, None), conflict(LEN - 1)),
    ];
    for (log, expected) in cases {
        assert_eq!(reconcile(&cp, &[], &log), expected, "{log:#?}");
    }
}
