//! A tool batch's results message, written as one entry or, with
//! `spec.tool_result_cap`, as two in one append.

use super::*;

fn entry_in(message: &Message, visible: bool, historical: bool) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Arc::new(message.clone()),
        visible,
        historical,
    })
}

#[test]
fn a_capped_results_message_is_appended_whole_or_not_at_all() {
    let pending = Message::user_text("results");
    let capped = Message::user_text("results, cut");
    let cp = checkpoint(Phase::ToolResults {
        message: pending.clone(),
    });
    let recovery = |appended| {
        Ok(Recovery::ToolResults {
            message: pending.clone(),
            appended,
        })
    };
    let two = |whole: LogEntry, shown: LogEntry| {
        let mut log = session(LEN, None);
        log.extend([whole, shown]);
        log
    };
    let cases = [
        (session(LEN, None), recovery(false)),
        (
            two(
                entry_in(&pending, false, true),
                entry_in(&capped, true, false),
            ),
            recovery(true),
        ),
        // The whole message first, in the audit only, and the cut one second.
        (
            two(
                entry_in(&pending, true, true),
                entry_in(&capped, true, false),
            ),
            conflict(LEN + 2),
        ),
        (
            two(
                entry_in(&capped, false, true),
                entry_in(&capped, true, false),
            ),
            conflict(LEN + 2),
        ),
        (
            two(
                entry_in(&pending, false, true),
                entry_in(&capped, true, true),
            ),
            conflict(LEN + 2),
        ),
    ];
    for (log, expected) in cases {
        assert_eq!(reconcile(&cp, &[], &log), expected, "{} entries", log.len());
    }
}
