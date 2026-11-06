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
