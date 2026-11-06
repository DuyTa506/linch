//! The single append-only session log: one source of truth, two projections.
//!
//! A [`SessionLog`] holds one ordered sequence of [`LogEntry`] values and
//! projects two read views from it, matching `src/linch/session_log.py`
//! (invariant 1 in `docs/architecture/invariants.md`):
//!
//! - [`SessionLog::full_history`] — every message entry appended with
//!   `historical: true`. A projection never changes it (invariant 2).
//! - [`SessionLog::provider_view`] — every message entry appended with
//!   `visible: true`, with each recorded [`ProjectionEntry`] replacing the
//!   accumulated view by its `replacement` (invariant 18: a projection is a
//!   logged, copy-on-write fact, never an in-place mutation).
//!
//! No method mutates or removes an existing entry; `append` and
//! `record_projection` only ever add one. Every appended message is wrapped
//! in an `Arc` once; `full_history`/`provider_view` hand out a clone of the
//! log's own `Arc`, so a read is a pointer clone, never a copy of message
//! data (unlike the Python implementation, which must `deepcopy` on every
//! read because `Message.provider_metadata` is `Any`-typed there). A write
//! that races an outstanding snapshot clones only the pointer vector
//! (`Arc::make_mut`'s copy-on-write), not the messages themselves.

use std::sync::Arc;

use sha2::{Digest as _, Sha256};

use crate::canonical::{self, Digest};
use crate::message::Message;

#[cfg(test)]
mod tests;

/// One logged entry: a message, or a recorded compaction.
///
/// No API mutates or removes an entry; a [`SessionLog`] only ever appends
/// one of these.
#[derive(Debug, Clone, PartialEq)]
pub enum LogEntry {
    /// A message appended to one or both projections.
    Message(MessageEntry),
    /// A logged compaction: the provider view becomes `replacement`.
    Projection(ProjectionEntry),
}

/// A logged message, optionally excluded from one projection.
///
/// `visible` places it in [`SessionLog::provider_view`]; `historical` places
/// it in [`SessionLog::full_history`]. A normal message is both; a
/// provider-view-only injection (a re-surfaced skill reminder) is
/// `historical: false`.
#[derive(Debug, Clone, PartialEq)]
pub struct MessageEntry {
    /// The logged message.
    pub message: Arc<Message>,
    /// Whether the message belongs in the provider projection.
    pub visible: bool,
    /// Whether the message belongs in the audit projection.
    pub historical: bool,
}

/// A logged compaction: the provider view becomes exactly `replacement`.
///
/// Subsequent visible message entries append after it. `full_history` is
/// unaffected — compaction never drops the audit record.
#[derive(Debug, Clone, PartialEq)]
pub struct ProjectionEntry {
    /// Messages that become the new provider projection.
    pub replacement: Vec<Arc<Message>>,
    /// Explanation for why the projection was recorded.
    pub reason: String,
}

/// Append-only log projecting the provider view and full history.
///
/// See the module documentation for the projection rules. `SessionLog` has
/// no interior mutability: every write takes `&mut self`, so the type is
/// `Send`/`Sync` whenever `Message` is.
#[derive(Default)]
pub struct SessionLog {
    entries: Vec<LogEntry>,
    full: Arc<Vec<Arc<Message>>>,
    derived: Arc<Vec<Arc<Message>>>,
    fingerprint: FingerprintState,
}

impl SessionLog {
    /// An empty log with no messages or projections.
    pub fn new() -> Self {
        Self::default()
    }

    /// Append one message, choosing which projections it joins.
    ///
    /// Args:
    ///     message: Message to add to the log.
    ///     visible: Whether to include the message in `provider_view`.
    ///     historical: Whether to include the message in `full_history`.
    pub fn append(&mut self, message: Message, visible: bool, historical: bool) {
        let message = Arc::new(message);
        if historical {
            Arc::make_mut(&mut self.full).push(Arc::clone(&message));
        }
        if visible {
            Arc::make_mut(&mut self.derived).push(Arc::clone(&message));
        }
        self.entries.push(LogEntry::Message(MessageEntry {
            message,
            visible,
            historical,
        }));
    }

    /// Replace the provider view and log the change.
    ///
    /// An equal replacement (by value, or by `Arc` identity for an unchanged
    /// element) is a no-op and is not recorded.
    ///
    /// Args:
    ///     replacement: Messages that should become the provider projection.
    ///     reason: Explanation stored with the projection entry.
    ///
    /// Returns:
    ///     `true` when a new projection entry was recorded; otherwise `false`.
    pub fn record_projection(
        &mut self,
        replacement: Vec<Arc<Message>>,
        reason: impl Into<String>,
    ) -> bool {
        if arc_messages_eq(&replacement, &self.derived) {
            return false;
        }
        self.entries.push(LogEntry::Projection(ProjectionEntry {
            replacement: replacement.clone(),
            reason: reason.into(),
        }));
        self.derived = Arc::new(replacement);
        true
    }

    /// An immutable inspection snapshot of the authoritative entries.
    ///
    /// Durable session persistence remains message/snapshot based; entries
    /// are not a durable projection journal.
    ///
    /// Returns:
    ///     Every logged entry, in append order.
    pub fn entries(&self) -> &[LogEntry] {
        &self.entries
    }

    /// Every historical message, in append order.
    ///
    /// Cloning the returned `Arc` is a pointer clone; it never copies
    /// message data.
    pub fn full_history(&self) -> Arc<Vec<Arc<Message>>> {
        Arc::clone(&self.full)
    }

    /// The current provider view: the latest projection's replacement plus
    /// later visible messages.
    ///
    /// Cloning the returned `Arc` is a pointer clone; it never copies
    /// message data.
    pub fn provider_view(&self) -> Arc<Vec<Arc<Message>>> {
        Arc::clone(&self.derived)
    }

    /// How many messages the provider view holds.
    pub fn visible_count(&self) -> usize {
        self.derived.len()
    }

    /// How many messages full history holds.
    pub fn history_count(&self) -> usize {
        self.full.len()
    }

    /// The newest provider-view message, or `None` when the view is empty.
    pub fn last_visible(&self) -> Option<&Message> {
        self.derived.last().map(Arc::as_ref)
    }

    /// Build a log from prior state (resume / subagent seed).
    ///
    /// `historical` seeds the full history; `visible` (when it differs)
    /// records a projection so the provider view starts compacted. New
    /// appends and projections extend this log normally.
    ///
    /// Args:
    ///     historical: Existing audit messages to seed, if any.
    ///     visible: Existing provider projection, if any.
    ///
    /// Returns:
    ///     A new session log initialized from the supplied projections.
    pub fn seed(historical: Vec<Message>, visible: Option<Vec<Message>>) -> Self {
        let mut log = Self::new();
        for message in historical {
            log.append(message, true, true);
        }
        if let Some(visible) = visible {
            log.record_projection(visible.into_iter().map(Arc::new).collect(), "");
        }
        log
    }

    /// A stable SHA-256 fingerprint of `full_history`.
    ///
    /// Equals [`canonical::digest`] of the JSON array of historical
    /// messages for any message within the depth ceiling (the incremental
    /// path below encodes each message at depth 0, one level shallower than
    /// its position inside that array, so a message whose own containers
    /// reach exactly [`canonical::MAX_ENCODE_DEPTH`] is the one case where
    /// this succeeds while `digest`-of-the-array would not). It folds only
    /// the tail added since the previous call, so repeated calls cost
    /// `O(new historical messages)`. A visible-only append or a projection
    /// never changes this value; only `historical` appends do.
    ///
    /// # Errors
    ///
    /// This can fail: returns [`canonical::CanonicalError::DepthExceeded`]
    /// if a message's JSON nests deeper than [`canonical::MAX_ENCODE_DEPTH`],
    /// the same way Python's `_history_fingerprint` raises past its own
    /// (smaller) depth limit of 100 (`_STRICT_JSON_MAX_DEPTH` in
    /// `src/linch/durability.py`). A failed call encodes nothing into the
    /// running hash and advances no counter, so a later call retries the
    /// same pending messages from scratch.
    pub fn history_fingerprint(&mut self) -> Result<Digest, canonical::CanonicalError> {
        let pending: Vec<Arc<Message>> = self.full[self.fingerprint.hashed_count..].to_vec();
        for message in &pending {
            let value = serde_json::to_value(message.as_ref())
                .expect("Message always serializes to valid JSON");
            // Encode before touching `self.fingerprint`: a failure here
            // must not leave a separator hashed in for a message that was
            // never actually counted.
            let encoded = canonical::to_canonical_string(&value)?;
            if self.fingerprint.hashed_count > 0 {
                self.fingerprint.hasher.update(b",");
            }
            self.fingerprint.hasher.update(encoded.as_bytes());
            self.fingerprint.hashed_count += 1;
        }
        let mut complete = self.fingerprint.hasher.clone();
        complete.update(b"]");
        Ok(Digest::from_bytes(complete.finalize().into()))
    }
}

/// Incremental SHA-256 state for [`SessionLog::history_fingerprint`].
///
/// `hasher` already has the leading `[` folded in; `hashed_count` is how
/// many of `SessionLog::full`'s messages are already folded into it.
struct FingerprintState {
    hasher: Sha256,
    hashed_count: usize,
}

impl Default for FingerprintState {
    fn default() -> Self {
        let mut hasher = Sha256::new();
        hasher.update(b"[");
        Self {
            hasher,
            hashed_count: 0,
        }
    }
}

/// Whether two `Arc<Message>` sequences hold equal messages, short-circuiting
/// on pointer identity before falling back to a value comparison.
fn arc_messages_eq(a: &[Arc<Message>], b: &[Arc<Message>]) -> bool {
    a.len() == b.len()
        && a.iter()
            .zip(b)
            .all(|(x, y)| Arc::ptr_eq(x, y) || **x == **y)
}
