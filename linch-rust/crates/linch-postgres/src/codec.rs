//! Conversions between linch-core values and their Postgres columns.
//!
//! JSON payloads are stored as compact JSON text and decoded strictly, so a
//! row that does not decode is an error, never silently dropped data.

use std::sync::Arc;

use linch_core::{
    Checkpoint, Digest, Envelope, LogEntry, Message, MessageEntry, PrefixEntry, ProjectionEntry,
    RunId, RunStatus, StoreError,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// A value could not be encoded into, or decoded from, its column.
///
/// Stores report it inside [`StoreError::Backend`].
#[derive(Debug, Error)]
#[error("cannot {action} column {column}")]
pub struct CodecError {
    /// `encode` or `decode`.
    pub action: &'static str,
    /// The column that holds the value.
    pub column: &'static str,
    #[source]
    source: BoxError,
}

fn codec(action: &'static str, column: &'static str, source: impl Into<BoxError>) -> StoreError {
    StoreError::Backend(Box::new(CodecError {
        action,
        column,
        source: source.into(),
    }))
}

pub(crate) fn encode_err(column: &'static str) -> impl FnOnce(BoxError) -> StoreError {
    move |source| codec("encode", column, source)
}

pub(crate) fn decode_err(column: &'static str) -> impl FnOnce(BoxError) -> StoreError {
    move |source| codec("decode", column, source)
}

/// A database error.
pub(crate) fn backend(error: sqlx::Error) -> StoreError {
    StoreError::Backend(Box::new(error))
}

pub(crate) fn not_found(kind: &'static str, namespace: &str, id: &str) -> StoreError {
    StoreError::NotFound {
        kind,
        namespace: namespace.to_owned(),
        id: id.to_owned(),
    }
}

/// A count or seq as a `BIGINT`.
pub(crate) fn to_i64(value: u64, column: &'static str) -> Result<i64, StoreError> {
    i64::try_from(value).map_err(|e| encode_err(column)(e.into()))
}

/// A `BIGINT` read back as a count or seq; the schema keeps them >= 0.
pub(crate) fn to_u64(value: i64, column: &'static str) -> Result<u64, StoreError> {
    u64::try_from(value).map_err(|e| decode_err(column)(e.into()))
}

pub(crate) fn status_name(status: RunStatus) -> &'static str {
    match status {
        RunStatus::Pending => "pending",
        RunStatus::Running => "running",
        RunStatus::AwaitingApproval => "awaiting_approval",
        RunStatus::AwaitingInput => "awaiting_input",
        RunStatus::Completed => "completed",
        RunStatus::Failed => "failed",
        RunStatus::Aborted => "aborted",
    }
}

pub(crate) fn parse_status(name: &str) -> Result<RunStatus, StoreError> {
    serde_json::from_value(name.into()).map_err(|e| decode_err("status")(e.into()))
}

pub(crate) fn parse_contract(text: &str) -> Result<Digest, StoreError> {
    text.parse()
        .map_err(|e| decode_err("contract")(Box::new(e)))
}

pub(crate) fn encode_checkpoint(checkpoint: &Checkpoint) -> Result<String, StoreError> {
    serde_json::to_string(checkpoint).map_err(|e| encode_err("checkpoint")(e.into()))
}

pub(crate) fn decode_checkpoint(text: &str) -> Result<Checkpoint, StoreError> {
    serde_json::from_str(text).map_err(|e| decode_err("checkpoint")(e.into()))
}

/// Seqs and envelope texts, ready to bind as two arrays.
pub(crate) fn encode_events(events: &[Envelope]) -> Result<(Vec<i64>, Vec<String>), StoreError> {
    let mut seqs = Vec::with_capacity(events.len());
    let mut texts = Vec::with_capacity(events.len());
    for event in events {
        let value = event
            .to_value()
            .map_err(|e| encode_err("envelope")(e.into()))?;
        seqs.push(to_i64(event.seq, "seq")?);
        texts.push(value.to_string());
    }
    Ok((seqs, texts))
}

pub(crate) fn decode_event(text: &str) -> Result<Envelope, StoreError> {
    text.parse()
        .map_err(|e| decode_err("envelope")(Box::new(e)))
}

/// Check that `events` belong to `run_id` and continue `last_seq`, in the
/// same order as linch-core's memory store: per event, run id then seq.
pub(crate) fn check_events(
    run_id: &RunId,
    last_seq: u64,
    events: &[Envelope],
) -> Result<(), StoreError> {
    for (expected, event) in (last_seq + 1..).zip(events) {
        if event.run_id != *run_id {
            return Err(StoreError::Conflict {
                message: format!("event for run {} written to run {run_id}", event.run_id),
            });
        }
        if event.seq != expected {
            return Err(StoreError::SeqGap {
                expected,
                got: event.seq,
            });
        }
    }
    Ok(())
}

/// Wire form of a [`LogEntry`] (linch-core defines none yet).
#[derive(Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum EntryWire<M, P> {
    Message {
        message: M,
        visible: bool,
        historical: bool,
    },
    Projection {
        replacement: Vec<M>,
        reason: String,
    },
    Prefix(P),
}

pub(crate) fn encode_entry(entry: &LogEntry) -> Result<String, StoreError> {
    let wire = match entry {
        LogEntry::Message(m) => EntryWire::Message {
            message: &*m.message,
            visible: m.visible,
            historical: m.historical,
        },
        LogEntry::Projection(p) => EntryWire::Projection {
            replacement: p.replacement.iter().map(|m| &**m).collect(),
            reason: p.reason.clone(),
        },
        LogEntry::Prefix(p) => EntryWire::Prefix(p),
    };
    serde_json::to_string(&wire).map_err(|e| encode_err("entry")(e.into()))
}

pub(crate) fn decode_entry(text: &str) -> Result<LogEntry, StoreError> {
    let wire: EntryWire<Message, PrefixEntry> =
        serde_json::from_str(text).map_err(|e| decode_err("entry")(e.into()))?;
    Ok(match wire {
        EntryWire::Message {
            message,
            visible,
            historical,
        } => LogEntry::Message(MessageEntry {
            message: Arc::new(message),
            visible,
            historical,
        }),
        EntryWire::Projection {
            replacement,
            reason,
        } => LogEntry::Projection(ProjectionEntry {
            replacement: replacement.into_iter().map(Arc::new).collect(),
            reason,
        }),
        EntryWire::Prefix(prefix) => LogEntry::Prefix(prefix),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_names_round_trip_and_match_serde() {
        use RunStatus::*;
        let all = [
            Pending,
            Running,
            AwaitingApproval,
            AwaitingInput,
            Completed,
            Failed,
            Aborted,
        ];
        for status in all {
            let name = status_name(status);
            assert_eq!(parse_status(name).unwrap(), status);
            assert_eq!(serde_json::to_value(status).unwrap(), name);
        }
        assert!(parse_status("suspended").is_err());
    }

    /// A frozen prefix with one system block, one tool and one skill.
    fn prefix() -> PrefixEntry {
        let value = serde_json::json!({
            "system": [{"text": "Be brief.", "cacheable": true}],
            "tools": [{"name": "Read", "description": "Read.", "input_schema": {"type": "object"}}],
            "skills": [{"name": "verify", "digest": "0".repeat(64)}],
        });
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn log_entries_round_trip() {
        let message = Arc::new(Message::user_text("hi"));
        let entries = [
            LogEntry::Message(MessageEntry {
                message: Arc::clone(&message),
                visible: true,
                historical: false,
            }),
            LogEntry::Projection(ProjectionEntry {
                replacement: vec![message, Arc::new(Message::assistant_text("ok"))],
                reason: "compacted".into(),
            }),
            LogEntry::Prefix(prefix()),
        ];
        for entry in entries {
            let text = encode_entry(&entry).unwrap();
            assert_eq!(decode_entry(&text).unwrap(), entry, "{text}");
        }
    }

    #[test]
    fn log_entry_decoding_is_strict() {
        let message = serde_json::to_string(&Message::user_text("hi")).unwrap();
        let ok =
            format!(r#"{{"type":"message","message":{message},"visible":true,"historical":true}}"#);
        assert!(decode_entry(&ok).is_ok());
        let extra = ok.replace(r#""visible""#, r#""extra":1,"visible""#);
        let unknown = ok.replace(r#""type":"message""#, r#""type":"note""#);
        let missing = ok.replace(r#","historical":true"#, "");
        let prefix = encode_entry(&LogEntry::Prefix(prefix())).unwrap();
        let stray = prefix.replace(r#""tools""#, r#""extra":1,"tools""#);
        for bad in [extra, unknown, missing, stray, "not json".into()] {
            let error = decode_entry(&bad).unwrap_err();
            let StoreError::Backend(source) = error else {
                panic!("{bad}: {error:?}");
            };
            let codec = source.downcast_ref::<CodecError>().expect("a codec error");
            assert_eq!((codec.action, codec.column), ("decode", "entry"));
        }
    }

    #[test]
    fn out_of_range_integers_are_codec_errors() {
        assert!(matches!(
            to_i64(u64::MAX, "seq"),
            Err(StoreError::Backend(_))
        ));
        assert!(matches!(to_u64(-1, "seq"), Err(StoreError::Backend(_))));
        assert_eq!(to_i64(7, "seq").unwrap(), 7);
    }
}
