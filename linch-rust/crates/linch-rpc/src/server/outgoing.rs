//! What the writer task sends, and how it joins a run's envelopes.
//!
//! A run's task queues each envelope on its own, as soon as the run hands
//! it over (once it is durable, for a durable kind). The writer takes
//! everything already waiting before each flush and sends consecutive
//! envelopes of one run as one `run.event` notification, so a turn's
//! envelopes cost the client one line instead of one each. Nothing waits
//! to be joined: the writer joins only what is queued, and any other
//! message, or another run's envelope, closes the batch before it, so the
//! order on the wire is the order of the queue.

use linch::RunId;
use serde::Serialize;
use serde_json::value::RawValue;

use crate::message::Version;
use crate::protocol::method;

/// A batch is closed before its envelopes pass this many bytes; an
/// envelope larger than that goes alone.
pub(super) const BATCH_BYTES: usize = 64 * 1024;

/// One item for the writer task.
pub(crate) enum Outgoing {
    /// A whole message, encoded.
    Message(String),
    /// One envelope of a run, encoded.
    Envelope {
        run_id: RunId,
        envelope: Box<RawValue>,
    },
}

impl Outgoing {
    /// The line the writer would send for this item alone.
    #[cfg(test)]
    pub(crate) fn into_line(self) -> String {
        match self {
            Self::Message(line) => line,
            Self::Envelope { run_id, envelope } => {
                let mut batch = Batch::default();
                batch.push(run_id, envelope);
                batch.take().expect("one envelope")
            }
        }
    }
}

/// The `run.event` line, with its envelopes copied in as encoded.
#[derive(Serialize)]
struct EventLine<'a> {
    jsonrpc: Version,
    method: &'static str,
    params: EventParams<'a>,
}

#[derive(Serialize)]
struct EventParams<'a> {
    run_id: &'a RunId,
    envelopes: &'a [Box<RawValue>],
}

/// Consecutive envelopes of one run, waiting to go out as one line.
#[derive(Default)]
pub(super) struct Batch {
    run_id: Option<RunId>,
    envelopes: Vec<Box<RawValue>>,
    bytes: usize,
}

impl Batch {
    /// Add one envelope. Returns the batch it closes, as a line, when it
    /// belongs to another run or would make this batch too large.
    pub(super) fn push(&mut self, run_id: RunId, envelope: Box<RawValue>) -> Option<String> {
        let size = envelope.get().len();
        let closes = self.run_id.as_ref().is_some_and(|open| *open != run_id)
            || (!self.envelopes.is_empty() && self.bytes + size > BATCH_BYTES);
        let closed = if closes { self.take() } else { None };
        self.run_id = Some(run_id);
        self.envelopes.push(envelope);
        self.bytes += size;
        closed
    }

    /// The open batch as one `run.event` line; `None` when empty.
    pub(super) fn take(&mut self) -> Option<String> {
        let run_id = self.run_id.take()?;
        let line = EventLine {
            jsonrpc: Version::V2,
            method: method::RUN_EVENT,
            params: EventParams {
                run_id: &run_id,
                envelopes: &self.envelopes,
            },
        };
        // Raw values are valid JSON and a run id is a string.
        let line = serde_json::to_string(&line).expect("a run.event line encodes");
        self.envelopes.clear();
        self.bytes = 0;
        Some(line)
    }
}

#[cfg(test)]
mod tests {
    use linch::{Event, JsonValue};
    use serde_json::json;

    use super::*;
    use crate::message::Notification;
    use crate::protocol::RunEvent;

    fn envelope(run: &str, seq: u64) -> Box<RawValue> {
        let event = json!({"type": "error", "error": {"message": "x"}});
        let value = json!({"v": 1, "seq": seq, "run_id": run, "ts_ms": 7, "event": event});
        serde_json::value::to_raw_value(&value).unwrap()
    }

    fn decode(line: &str) -> RunEvent {
        let notification: Notification = serde_json::from_str(line).unwrap();
        assert_eq!(notification.method, method::RUN_EVENT);
        serde_json::from_value(notification.params).unwrap()
    }

    #[test]
    fn consecutive_envelopes_of_a_run_join_and_another_run_closes_the_batch() {
        let mut batch = Batch::default();
        assert!(batch.push("a".into(), envelope("a", 1)).is_none());
        assert!(batch.push("a".into(), envelope("a", 2)).is_none());
        let first = decode(&batch.push("b".into(), envelope("b", 1)).unwrap());
        assert_eq!(first.run_id, RunId::from("a"));
        let seqs: Vec<u64> = first.envelopes.iter().map(|e| e.seq).collect();
        assert_eq!(seqs, [1, 2]);
        assert!(matches!(first.envelopes[0].event, Event::Error(_)));
        let second = decode(&batch.take().unwrap());
        assert_eq!(second.run_id, RunId::from("b"));
        assert_eq!(second.envelopes.len(), 1);
        assert!(batch.take().is_none(), "the batch is empty after take");
    }

    #[test]
    fn a_batch_closes_before_it_passes_the_byte_cap() {
        let mut batch = Batch::default();
        let big = "x".repeat(BATCH_BYTES / 2);
        let large = |seq: u64| {
            let event = json!({"type": "error", "error": {"message": big}});
            let value = json!({"v": 1, "seq": seq, "run_id": "a", "ts_ms": 7, "event": event});
            serde_json::value::to_raw_value(&value).unwrap()
        };
        assert!(batch.push("a".into(), large(1)).is_none());
        let closed = batch
            .push("a".into(), large(2))
            .expect("the second would pass the cap");
        assert_eq!(decode(&closed).envelopes.len(), 1);
        let huge = serde_json::value::to_raw_value(&json!("y".repeat(BATCH_BYTES * 2))).unwrap();
        let closed = batch
            .push("a".into(), huge)
            .expect("closes the batch before it");
        assert_eq!(decode(&closed).envelopes[0].seq, 2);
        let alone: JsonValue = serde_json::from_str(&batch.take().unwrap()).unwrap();
        assert_eq!(alone["params"]["envelopes"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn each_joined_envelope_is_still_checked_for_its_version() {
        let mut batch = Batch::default();
        let future = json!({"v": 2, "seq": 1, "run_id": "a", "ts_ms": 7,
                            "event": {"type": "error", "error": {"message": "x"}}});
        batch.push(
            "a".into(),
            serde_json::value::to_raw_value(&future).unwrap(),
        );
        let notification: Notification = serde_json::from_str(&batch.take().unwrap()).unwrap();
        let refused = serde_json::from_value::<RunEvent>(notification.params).unwrap_err();
        assert!(refused.to_string().contains("version"), "{refused}");
    }
}
