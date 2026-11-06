//! The reconcile table: every phase against every store state that matters.

mod compaction;

use std::sync::Arc;

use super::*;
use crate::events::WIRE_VERSION;
use crate::events::kinds::{AssistantEvent, ToolCallEndEvent, ToolCallStartEvent};
use crate::ids::RunId;
use crate::json::JsonObject;
use crate::message::ContentBlock;
use crate::policy::PermissionDecision;
use crate::session_log::MessageEntry;
use crate::usage::Usage;

const LEN: u64 = 3;

type Outcome = Result<Recovery, ReconcileError>;

fn checkpoint(phase: Phase) -> Checkpoint {
    Checkpoint {
        turn: 2,
        usage: Usage::default(),
        usage_complete: true,
        cost_usd: None,
        session_len: LEN,
        phase,
    }
}

fn entry(message: &Message) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Arc::new(message.clone()),
        visible: true,
        historical: true,
    })
}

/// `len` filler entries, the last one replaced by `last` when given.
fn session(len: u64, last: Option<&Message>) -> Vec<LogEntry> {
    let mut log: Vec<LogEntry> = (0..len)
        .map(|i| entry(&Message::user_text(format!("m{i}"))))
        .collect();
    if let (Some(slot), Some(last)) = (log.last_mut(), last) {
        *slot = entry(last);
    }
    log
}

fn envelopes(events: Vec<Event>) -> Vec<Envelope> {
    (1..)
        .zip(events)
        .map(|(seq, event)| Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: RunId::new("r1"),
            ts_ms: 0,
            ignorable: false,
            event,
        })
        .collect()
}

fn call(id: &str, name: &str) -> ToolUseBlock {
    ToolUseBlock {
        id: id.into(),
        name: name.into(),
        input: JsonObject::new(),
    }
}

fn tool_message(calls: &[ToolUseBlock]) -> Message {
    let blocks = calls.iter().cloned().map(ContentBlock::ToolUse).collect();
    Message::new(Role::Assistant, blocks)
}

fn start(id: &str, name: &str) -> Event {
    Event::ToolCallStart(ToolCallStartEvent {
        tool_use_id: id.into(),
        tool_name: name.into(),
        input: JsonObject::new(),
        summary: String::new(),
    })
}

fn end(id: &str, name: &str) -> Event {
    Event::ToolCallEnd(ToolCallEndEvent {
        tool_name: name.into(),
        result: ToolResultBlock::text(id, format!("{id} ok"), false),
        duration_ms: 1,
        canonical_output: None,
    })
}

fn delivery(turn: u64, attempt: &str) -> Event {
    Event::ModelCallDelivery(ModelCallDeliveryEvent {
        run_id: RunId::new("r1"),
        logical_call_id: "lc2".into(),
        delivery_attempt_id: attempt.into(),
        kind: ModelCallKind::Primary,
        turn_index: Some(turn),
        requested_model: "m".into(),
    })
}

fn receipt(attempt: &str, disposition: CallDisposition) -> Event {
    let Event::ModelCallDelivery(d) = delivery(2, attempt) else {
        unreachable!()
    };
    Event::ModelCallReceipt(ModelCallReceiptEvent {
        disposition,
        duration_ms: 5,
        ..abandoned(&d)
    })
}

fn response(message: &Message) -> Event {
    Event::Assistant(AssistantEvent {
        message: message.clone(),
        stop_reason: StopReason::EndTurn,
    })
}

fn conflict(found: u64) -> Outcome {
    Err(ReconcileError::SessionConflict {
        expected: LEN,
        found,
    })
}

#[test]
fn a_pending_session_append_is_checked_against_the_log() {
    let pending = Message::user_text("pending");
    let other = Message::user_text("other");
    type Expect = fn(bool) -> Recovery;
    let phases: [(Phase, Expect); 2] = [
        (
            Phase::AssistantReceived {
                message: pending.clone(),
                stop_reason: StopReason::EndTurn,
            },
            |appended| Recovery::Received {
                message: Message::user_text("pending"),
                stop_reason: StopReason::EndTurn,
                appended,
            },
        ),
        (
            Phase::ToolResults {
                message: pending.clone(),
            },
            |appended| Recovery::ToolResults {
                message: Message::user_text("pending"),
                appended,
            },
        ),
    ];
    for (phase, recovery) in phases {
        let cp = checkpoint(phase);
        let cases = [
            (session(LEN, None), Ok(recovery(false))),
            (session(LEN + 1, Some(&pending)), Ok(recovery(true))),
            (session(LEN + 1, Some(&other)), conflict(LEN + 1)),
            (session(LEN + 2, Some(&pending)), conflict(LEN + 2)),
            (session(LEN - 1, None), conflict(LEN - 1)),
        ];
        for (log, expected) in cases {
            assert_eq!(reconcile(&cp, &[], &log), expected, "{:?}", cp.phase);
        }
    }
}

#[test]
fn a_started_prompt_goes_after_runs_that_ended_before_it() {
    let prompt = Message::user_text("pending");
    let other = Message::user_text("other");
    let cp = checkpoint(Phase::Started {
        prompt: prompt.clone(),
    });
    let started = |appended| {
        Ok(Recovery::Started {
            prompt: prompt.clone(),
            appended,
        })
    };
    let cases = [
        (session(LEN, None), started(false)),
        (session(LEN + 1, Some(&prompt)), started(true)),
        // Entries of runs that ended before this one was created.
        (session(LEN + 1, Some(&other)), started(false)),
        (session(LEN + 2, Some(&prompt)), started(false)),
        (session(LEN - 1, None), conflict(LEN - 1)),
    ];
    for (log, expected) in cases {
        assert_eq!(reconcile(&cp, &[], &log), expected, "{} entries", log.len());
    }
}

#[test]
fn provider_pending_retries_or_reuses_the_receipt() {
    let cp = checkpoint(Phase::ProviderPending { attempt: 2 });
    let assistant = Message::assistant_text("answer");
    let log = session(LEN, None);
    let d = match delivery(2, "a2") {
        Event::ModelCallDelivery(d) => d,
        _ => unreachable!(),
    };
    let redispatch = |abandoned| {
        Ok(Recovery::Redispatch {
            attempt: 3,
            abandoned,
        })
    };
    let received = |appended| {
        Ok(Recovery::Received {
            message: Message::assistant_text("answer"),
            stop_reason: StopReason::EndTurn,
            appended,
        })
    };
    let missing = Err(ReconcileError::MissingDelivery { turn: 2 });
    let cases: Vec<(Vec<Event>, Vec<LogEntry>, Outcome)> = vec![
        (vec![], log.clone(), missing.clone()),
        (vec![delivery(1, "a1")], log.clone(), missing),
        (
            vec![delivery(2, "a2")],
            log.clone(),
            redispatch(Some(abandoned(&d))),
        ),
        (
            vec![delivery(2, "a2")],
            session(LEN + 1, None),
            conflict(LEN + 1),
        ),
        (
            vec![
                delivery(2, "a1"),
                receipt("a1", CallDisposition::Failed),
                delivery(2, "a2"),
            ],
            log.clone(),
            redispatch(Some(abandoned(&d))),
        ),
        (
            vec![delivery(2, "a2"), receipt("a2", CallDisposition::Failed)],
            log.clone(),
            redispatch(None),
        ),
        (
            vec![delivery(2, "a2"), receipt("a2", CallDisposition::Cancelled)],
            log.clone(),
            redispatch(None),
        ),
        (
            vec![
                delivery(2, "a2"),
                receipt("a2", CallDisposition::Accepted),
                response(&assistant),
            ],
            log.clone(),
            received(false),
        ),
        (
            vec![
                delivery(2, "a2"),
                receipt("a2", CallDisposition::Accepted),
                response(&assistant),
            ],
            session(LEN + 1, Some(&assistant)),
            received(true),
        ),
        (
            vec![
                delivery(2, "a2"),
                receipt("a2", CallDisposition::Accepted),
                response(&assistant),
            ],
            session(LEN + 1, None),
            conflict(LEN + 1),
        ),
        (
            vec![delivery(2, "a2"), receipt("a2", CallDisposition::Accepted)],
            log.clone(),
            Err(ReconcileError::MissingAssistant {
                delivery_attempt_id: "a2".into(),
            }),
        ),
    ];
    for (events, log, expected) in cases {
        let events = envelopes(events);
        assert_eq!(reconcile(&cp, &events, &log), expected, "{events:#?}");
    }
    let receipt = abandoned(&d);
    assert_eq!(receipt.disposition, CallDisposition::Cancelled);
    assert_eq!(
        (receipt.usage, receipt.duration_ms),
        (None, 0),
        "unknown usage"
    );
}

#[test]
fn a_tool_batch_reuses_ends_and_never_reruns_a_started_tool() {
    let calls = [call("c1", "Read"), call("c2", "Read"), call("c3", "Write")];
    let decisions = vec![ToolDecisionRecord {
        tool_use_id: "c1".into(),
        decision: PermissionDecision::Allow {},
    }];
    let cp = checkpoint(Phase::ToolBatch {
        decisions: decisions.clone(),
        after_seq: 2,
    });
    let log = session(LEN, Some(&tool_message(&calls)));
    let events = envelopes(vec![
        start("c3", "Write"), // at or before after_seq: an earlier turn
        end("c3", "Write"),
        end("c1", "Read"), // an end before its start does not count
        start("c1", "Read"),
        start("c2", "Read"),
        end("c1", "Read"),
        start("c3", "Other"), // same id, another tool
    ]);
    let expected = Recovery::ToolBatch {
        decisions,
        calls: vec![
            ToolRecovery::Done(ToolResultBlock::text("c1", "c1 ok", false)),
            ToolRecovery::Interrupted {
                tool_name: "Read".into(),
                result: ToolResultBlock::text("c2", INTERRUPTED_TOOL_TEXT, true),
            },
            ToolRecovery::Replan(call("c3", "Write")),
        ],
    };
    assert_eq!(reconcile(&cp, &events, &log), Ok(expected));

    assert_eq!(
        reconcile(&cp, &events, &session(LEN + 1, None)),
        conflict(LEN + 1)
    );
    let no_calls = session(LEN, Some(&Message::assistant_text("no tools")));
    let user_last = session(LEN, None);
    for log in [no_calls, user_last] {
        let result = reconcile(&cp, &events, &log);
        assert_eq!(result, Err(ReconcileError::MissingToolCalls));
    }
}

#[test]
fn awaiting_approval_keeps_waiting() {
    let calls = [call("c1", "Write")];
    let cp = checkpoint(Phase::AwaitingApproval {
        decisions: Vec::new(),
        pending: vec!["c1".into()],
    });
    let log = session(LEN, Some(&tool_message(&calls)));
    let expected = Recovery::AwaitingApproval {
        decisions: Vec::new(),
        pending: vec!["c1".into()],
    };
    assert_eq!(reconcile(&cp, &[], &log), Ok(expected));
    assert_eq!(
        reconcile(&cp, &[], &session(LEN - 1, None)),
        conflict(LEN - 1)
    );
    let result = reconcile(&cp, &[], &session(LEN, None));
    assert_eq!(result, Err(ReconcileError::MissingToolCalls));
}
