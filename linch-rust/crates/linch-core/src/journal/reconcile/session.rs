//! Session checks of reconciliation: does the session log fit the
//! checkpoint, and is the phase's pending entry already in it.

use super::ReconcileError;
use crate::message::Message;
use crate::run::Checkpoint;
use crate::session_log::LogEntry;

/// The session must have exactly `session_len` entries.
pub(super) fn unchanged(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
) -> Result<(), ReconcileError> {
    let found = session.len() as u64;
    if found == checkpoint.session_len {
        return Ok(());
    }
    Err(ReconcileError::SessionConflict {
        expected: checkpoint.session_len,
        found,
    })
}

/// Whether `pending` was already appended: `session_len` entries means not
/// yet, one more with `pending` last means yes, anything else conflicts.
pub(super) fn appended(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
    pending: &Message,
) -> Result<bool, ReconcileError> {
    let found = session.len() as u64;
    if found == checkpoint.session_len {
        return Ok(false);
    }
    match session.last() {
        Some(LogEntry::Message(entry))
            if found == checkpoint.session_len + 1 && *entry.message == *pending =>
        {
            Ok(true)
        }
        _ => Err(ReconcileError::SessionConflict {
            expected: checkpoint.session_len,
            found,
        }),
    }
}

/// Whether a tool batch's results message `pending` was already appended:
/// like [`appended()`], or as the two entries that `spec.tool_result_cap`
/// writes in one append, the whole message for the audit projection only and
/// the cut one for the provider view only, which lands whole or not at all.
pub(super) fn results_appended(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
    pending: &Message,
) -> Result<bool, ReconcileError> {
    let start = usize::try_from(checkpoint.session_len).unwrap_or(usize::MAX);
    if let Some([LogEntry::Message(whole), LogEntry::Message(shown)]) = session.get(start..)
        && *whole.message == *pending
        && (!whole.visible && whole.historical)
        && (shown.visible && !shown.historical)
    {
        return Ok(true);
    }
    appended(checkpoint, session, pending)
}

/// Whether a `Started` run's `prompt` was already appended: like
/// [`appended()`], except that a session that grew by other entries is not
/// a conflict. The store admits one live run per session, so those entries
/// came from runs that ended before this run was created, and the prompt is
/// not in the session yet. A session's first prompt lands after its frozen
/// prefix, in the same write.
pub(super) fn prompt_appended(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
    prompt: &Message,
) -> Result<bool, ReconcileError> {
    let found = session.len() as u64;
    if found < checkpoint.session_len {
        return Err(ReconcileError::SessionConflict {
            expected: checkpoint.session_len,
            found,
        });
    }
    let start = usize::try_from(checkpoint.session_len).unwrap_or(usize::MAX);
    Ok(match session.get(start..).unwrap_or_default() {
        [LogEntry::Message(entry)] | [LogEntry::Prefix(_), LogEntry::Message(entry)] => {
            *entry.message == *prompt
        }
        _ => false,
    })
}

/// Whether a micro-compaction's (or a clip's) projection was already appended: like
/// [`appended()`], for a trailing projection recorded as one.
pub(super) fn cleared(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
) -> Result<bool, ReconcileError> {
    let found = session.len() as u64;
    if found == checkpoint.session_len {
        return Ok(false);
    }
    match session.last() {
        Some(LogEntry::Projection(entry))
            if found == checkpoint.session_len + 1
                && [
                    crate::compaction::MICRO_REASON,
                    crate::compaction::CLIP_REASON,
                ]
                .contains(&entry.reason.as_str()) =>
        {
            Ok(true)
        }
        _ => Err(ReconcileError::SessionConflict {
            expected: checkpoint.session_len,
            found,
        }),
    }
}

/// Whether the compaction projection was already appended: like
/// [`appended()`], for a trailing projection whose summary is `summary`.
pub(super) fn projected(
    checkpoint: &Checkpoint,
    session: &[LogEntry],
    summary: &Message,
) -> Result<bool, ReconcileError> {
    let found = session.len() as u64;
    if found == checkpoint.session_len {
        return Ok(false);
    }
    match session.last() {
        Some(LogEntry::Projection(entry))
            if found == checkpoint.session_len + 1
                && entry.replacement.get(1).is_some_and(|m| **m == *summary) =>
        {
            Ok(true)
        }
        _ => Err(ReconcileError::SessionConflict {
            expected: checkpoint.session_len,
            found,
        }),
    }
}
