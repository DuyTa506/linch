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
fn a_pending_summary_is_asked_again_and_a_settled_one_never_is() {
    let cp = checkpoint(Phase::CompactionPending { attempt: 1 });
    let log = session(LEN, None);
    let Event::ModelCallDelivery(d) = summary_delivery(2, "c1") else {
        unreachable!()
    };
    let recompact = |abandoned| {
        Ok(Recovery::Recompact {
            attempt: 2,
            abandoned,
            redispatch: None,
        })
    };
    // A summary that failed or gave no text: on to the primary call.
    let dispatch = Ok(Recovery::Redispatch {
        attempt: 1,
        abandoned: None,
    });
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
            dispatch.clone(),
        ),
        (
            vec![
                summary_delivery(2, "c1"),
                summary_receipt("c1", CallDisposition::Accepted),
            ],
            log.clone(),
            dispatch,
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
            attempt: 1,
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

/// A summary after the turn's primary call was forced by a context-length
/// refusal: a resume asks for it again and then calls the primary call's
/// next attempt, never attempt 1 again; one that settled without a summary
/// ends the run instead of calling with the refused prompt.
#[test]
fn a_forced_summary_resumes_forced() {
    let cp = checkpoint(Phase::CompactionPending { attempt: 1 });
    let log = session(LEN, None);
    let refused = vec![
        delivery(2, "p1"),
        receipt("p1", CallDisposition::Failed),
        delivery(2, "p2"),
        receipt("p2", CallDisposition::Failed),
        summary_delivery(2, "c1"),
    ];
    let Event::ModelCallDelivery(d) = summary_delivery(2, "c1") else {
        unreachable!()
    };
    assert_eq!(
        reconcile(&cp, &envelopes(refused.clone()), &log),
        Ok(Recovery::Recompact {
            attempt: 2,
            abandoned: Some(abandoned(&d)),
            redispatch: Some(3),
        })
    );
    let mut settled = refused.clone();
    settled.push(summary_receipt("c1", CallDisposition::Failed));
    assert_eq!(
        reconcile(&cp, &envelopes(settled), &log),
        Ok(Recovery::Unshortened)
    );
    // A turn-start summary of the next turn is not forced.
    let next = checkpoint(Phase::CompactionPending { attempt: 1 });
    let next = Checkpoint { turn: 3, ..next };
    let mut later = refused;
    later.push(summary_receipt("c1", CallDisposition::Accepted));
    later.push(delivery(2, "p3"));
    later.push(summary_delivery(3, "c1"));
    let Event::ModelCallDelivery(d) = summary_delivery(3, "c1") else {
        unreachable!()
    };
    assert_eq!(
        reconcile(&next, &envelopes(later), &log),
        Ok(Recovery::Recompact {
            attempt: 2,
            abandoned: Some(abandoned(&d)),
            redispatch: None,
        })
    );
}

/// The `compaction` event that records a forced summary refused as too long.
fn refusal() -> Event {
    Event::Compaction(crate::events::kinds::CompactionEvent {
        messages_before: 9,
        messages_after: 9,
        tokens_before: 100,
        tokens_after: 100,
        strategy: "summary".into(),
        duration_ms: 0,
        forced: true,
        rejected: Some("context_length: the summary request was refused".into()),
        measured_tokens: None,
        clipped_bytes: Some(10),
    })
}

/// A forced summary the provider refused as too long goes again, on less of
/// the older part, until it has made all its tries; a refusal of any other
/// kind ends the run.
#[test]
fn a_forced_summary_refused_as_too_long_goes_again_until_its_tries_are_spent() {
    let cp = checkpoint(Phase::CompactionPending { attempt: 1 });
    let log = session(LEN, None);
    let mut events = vec![
        delivery(2, "p1"),
        receipt("p1", CallDisposition::Failed),
        summary_delivery(2, "c1"),
        summary_receipt("c1", CallDisposition::Failed),
    ];
    // Failed with no refusal recorded: the run ends.
    assert_eq!(
        reconcile(&cp, &envelopes(events.clone()), &log),
        Ok(Recovery::Unshortened)
    );
    events.push(refusal());
    assert_eq!(
        reconcile(&cp, &envelopes(events.clone()), &log),
        Ok(Recovery::Recompact {
            attempt: 2,
            abandoned: None,
            redispatch: Some(2),
        })
    );
    // Three more refused tries: the fourth is the last.
    for n in 2..=4 {
        let id = format!("c{n}");
        events.push(summary_delivery(2, &id));
        events.push(summary_receipt(&id, CallDisposition::Failed));
        events.push(refusal());
        let cp = checkpoint(Phase::CompactionPending { attempt: n });
        let expected = match n {
            4 => Ok(Recovery::Unshortened),
            _ => Ok(Recovery::Recompact {
                attempt: n + 1,
                abandoned: None,
                redispatch: Some(2),
            }),
        };
        assert_eq!(reconcile(&cp, &envelopes(events.clone()), &log), expected);
    }
}

#[test]
fn a_forced_summary_is_followed_by_the_next_primary_attempt() {
    let summary = Message::user_text("<summary>");
    let cp = checkpoint(Phase::Compacted {
        summary: summary.clone(),
        keep_from: 5,
    });
    let events = envelopes(vec![
        delivery(2, "p1"),
        receipt("p1", CallDisposition::Failed),
        summary_delivery(2, "c1"),
        summary_receipt("c1", CallDisposition::Accepted),
    ]);
    assert_eq!(
        reconcile(&cp, &events, &session(LEN, None)),
        Ok(Recovery::Compacted {
            summary,
            keep_from: 5,
            appended: false,
            attempt: 2,
        })
    );
}

/// A clip's projection carries its own reason, and its budget rides the
/// recovery so a resume clips the same way.
#[test]
fn a_clipped_view_is_projected_once() {
    let cp = checkpoint(Phase::Cleared {
        before: 5,
        attempt: 2,
        clip: Some(4000),
    });
    let clipped = |appended| {
        Ok(Recovery::Cleared {
            before: 5,
            attempt: 2,
            clip: Some(4000),
            appended,
        })
    };
    let mut log = session(LEN, None);
    assert_eq!(reconcile(&cp, &[], &log), clipped(false));
    log.push(LogEntry::Projection(ProjectionEntry {
        replacement: vec![Arc::new(Message::user_text("m0"))],
        reason: "clip".into(),
    }));
    assert_eq!(reconcile(&cp, &[], &log), clipped(true));
}

#[test]
fn a_cleared_view_is_projected_once() {
    let cp = checkpoint(Phase::Cleared {
        before: 5,
        attempt: 2,
        clip: None,
    });
    let cleared = |appended| {
        Ok(Recovery::Cleared {
            before: 5,
            attempt: 2,
            clip: None,
            appended,
        })
    };
    let micro = |reason: &str| {
        let mut log = session(LEN, None);
        log.push(LogEntry::Projection(ProjectionEntry {
            replacement: vec![Arc::new(Message::user_text("m0"))],
            reason: reason.into(),
        }));
        log
    };
    let cases: Vec<(Vec<LogEntry>, Outcome)> = vec![
        (session(LEN, None), cleared(false)),
        (micro("micro-compaction"), cleared(true)),
        (micro("compaction"), conflict(LEN + 1)),
        (session(LEN + 1, None), conflict(LEN + 1)),
        (session(LEN - 1, None), conflict(LEN - 1)),
    ];
    for (log, expected) in cases {
        assert_eq!(reconcile(&cp, &[], &log), expected, "{log:#?}");
    }
}
