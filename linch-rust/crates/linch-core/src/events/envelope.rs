//! The durable wire envelope around one [`super::Event`], and the typed
//! decode error shared by the envelope and the event dispatcher.

use super::event::Event;
use super::wire::{
    reject_unknown_fields, take_bool_default_false, take_object, take_string, take_u64,
};
use crate::ids::RunId;
use crate::json::JsonValue;
use serde::{Deserialize, Serialize};

/// Wire version this crate reads and writes. A stored or received envelope
/// with a different `v` is rejected rather than guessed at.
pub const WIRE_VERSION: u32 = 1;

/// Upper bound on how many [`super::kinds::SubagentEvent`]s may nest inside
/// one another before decoding fails.
///
/// Practical nesting is depth 1 (a subagent cannot itself spawn a
/// subagent), so this is defensive headroom against a malformed or
/// adversarial stream, not a derived requirement.
pub(super) const MAX_SUBAGENT_DEPTH: usize = 8;

/// Decode-time context threaded through [`Event::decode`].
///
/// `ignorable` comes from the enclosing [`Envelope`] and is unchanged by
/// nesting; `depth` counts subagent nesting and is bounded by
/// [`MAX_SUBAGENT_DEPTH`].
#[derive(Debug, Clone, Copy)]
pub(super) struct DecodeCtx {
    pub(super) ignorable: bool,
    pub(super) depth: usize,
}

impl DecodeCtx {
    /// Context for decoding a bare event with no enclosing envelope: not
    /// ignorable, at the root depth.
    pub(super) fn strict() -> Self {
        Self {
            ignorable: false,
            depth: 0,
        }
    }

    /// Context for decoding the event directly inside an envelope.
    pub(super) fn root(ignorable: bool) -> Self {
        Self {
            ignorable,
            depth: 0,
        }
    }

    /// Context for decoding one level deeper, inside a `subagent` event.
    pub(super) fn nested(self) -> Result<Self, EventError> {
        let depth = self.depth + 1;
        if depth > MAX_SUBAGENT_DEPTH {
            return Err(EventError::NestingTooDeep {
                depth,
                max: MAX_SUBAGENT_DEPTH,
            });
        }
        Ok(Self {
            ignorable: self.ignorable,
            depth,
        })
    }
}

/// Every way decoding an [`Envelope`] or [`Event`] can fail.
#[derive(Debug, thiserror::Error)]
pub enum EventError {
    /// The value at this position was not a JSON object.
    #[error("expected an object, found {0}")]
    NotAnObject(&'static str),
    /// A required field was missing.
    #[error("missing required field {0:?}")]
    MissingField(&'static str),
    /// A field was present but had the wrong JSON type.
    #[error("field {field:?} must be {expected}, found {found}")]
    InvalidField {
        /// Name of the offending field.
        field: &'static str,
        /// What the field was expected to be.
        expected: &'static str,
        /// What it actually was.
        found: &'static str,
    },
    /// The envelope's `v` did not match [`WIRE_VERSION`].
    #[error("unsupported wire version {found}; this build reads version {expected}")]
    UnsupportedVersion {
        /// The version found on the wire.
        found: u64,
        /// The version this build reads ([`WIRE_VERSION`]).
        expected: u32,
    },
    /// A field was present that none of this type's known fields use.
    #[error("unexpected field {0:?}")]
    UnknownField(String),
    /// The event's `type` was not one this build knows, and the enclosing
    /// envelope did not mark it `ignorable`.
    #[error("unknown event type {0:?}; the envelope must be ignorable to allow it")]
    UnknownType(String),
    /// Subagent nesting exceeded `MAX_SUBAGENT_DEPTH`.
    #[error("event nesting depth {depth} exceeds the maximum of {max}")]
    NestingTooDeep {
        /// The depth that was reached.
        depth: usize,
        /// The maximum allowed depth.
        max: usize,
    },
    /// [`super::IgnorableEvent::new`] was called with a type this build
    /// already knows.
    #[error("event type {0:?} is known and cannot be wrapped as ignorable")]
    KnownTypeIgnorable(String),
    /// [`super::IgnorableEvent::new`] was called with an empty type name.
    #[error("an ignorable event type must be a non-empty string")]
    EmptyIgnorableType,
    /// A known event's payload failed to deserialize.
    #[error("invalid payload for event type {type_name:?}: {source}")]
    InvalidPayload {
        /// The event type whose payload was invalid.
        type_name: String,
        /// The underlying deserialization error.
        #[source]
        source: serde_json::Error,
    },
}

/// The durable envelope around one event.
///
/// `seq` is a monotonic sequence number assigned by the producer so a
/// client can resume a stream from a known point; ordering it is the
/// caller's job, but carrying it is mandatory on every envelope. `ignorable`
/// is the producer's promise that an unknown `event.type` is safe to skip
/// (see `Event::decode` and `docs/architecture/events.md`); it does not
/// rescue a malformed *known* event.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Envelope {
    /// Wire version this envelope was written with.
    pub v: u32,
    /// Monotonic sequence number, assigned by the producer.
    pub seq: u64,
    /// Run this envelope belongs to.
    pub run_id: RunId,
    /// Whether an unknown `event.type` should decode to
    /// [`super::IgnorableEvent`] instead of failing.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub ignorable: bool,
    /// The event itself.
    pub event: Event,
}

impl Envelope {
    /// Decode an envelope from an owned JSON value.
    ///
    /// Args:
    ///     value: The envelope's JSON representation.
    ///
    /// Returns:
    ///     The decoded envelope.
    ///
    /// Raises:
    ///     EventError: If `v` is missing or unsupported, a required field is
    ///         missing or malformed, an unexpected field is present, or the
    ///         nested event fails to decode.
    pub fn from_value(value: JsonValue) -> Result<Self, EventError> {
        let mut obj = take_object(value)?;
        let v = take_u64(&mut obj, "v")?;
        if v != WIRE_VERSION as u64 {
            return Err(EventError::UnsupportedVersion {
                found: v,
                expected: WIRE_VERSION,
            });
        }
        let seq = take_u64(&mut obj, "seq")?;
        let run_id = RunId::from(take_string(&mut obj, "run_id")?);
        let ignorable = take_bool_default_false(&mut obj, "ignorable")?;
        let event_value = obj
            .remove("event")
            .ok_or(EventError::MissingField("event"))?;
        reject_unknown_fields(obj)?;
        let event = Event::decode(event_value, DecodeCtx::root(ignorable))?;
        Ok(Envelope {
            v: v as u32,
            seq,
            run_id,
            ignorable,
            event,
        })
    }
}

impl std::str::FromStr for Envelope {
    type Err = EventError;

    /// Decode an envelope from its JSON text representation.
    ///
    /// Raises:
    ///     EventError: If `text` is not valid JSON, or [`Envelope::from_value`]
    ///         rejects the parsed value.
    fn from_str(text: &str) -> Result<Self, EventError> {
        let value: JsonValue =
            serde_json::from_str(text).map_err(|source| EventError::InvalidPayload {
                type_name: "<envelope>".to_string(),
                source,
            })?;
        Self::from_value(value)
    }
}

impl<'de> Deserialize<'de> for Envelope {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = JsonValue::deserialize(deserializer)?;
        Envelope::from_value(value).map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn minimal_known_event() -> JsonValue {
        json!({"type": "error", "error": {}})
    }

    #[test]
    fn wrong_version_is_rejected() {
        let value = json!({"v": 2, "seq": 1, "run_id": "r1", "event": minimal_known_event()});
        let err = Envelope::from_value(value).unwrap_err();
        assert!(matches!(
            err,
            EventError::UnsupportedVersion { found: 2, .. }
        ));
    }

    #[test]
    fn unknown_type_is_rejected_when_not_ignorable() {
        let value = json!({
            "v": WIRE_VERSION, "seq": 1, "run_id": "r1",
            "event": {"type": "from_the_future", "x": 1},
        });
        let err = Envelope::from_value(value).unwrap_err();
        assert!(matches!(err, EventError::UnknownType(ref t) if t == "from_the_future"));
    }

    #[test]
    fn unknown_type_round_trips_as_ignorable_when_flagged() {
        let value = json!({
            "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true,
            "event": {"type": "from_the_future", "x": 1},
        });
        let envelope = Envelope::from_value(value.clone()).unwrap();
        assert_eq!(serde_json::to_value(&envelope).unwrap(), value);
    }

    #[test]
    fn malformed_known_event_is_rejected_even_when_ignorable() {
        let value = json!({
            "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true,
            // "error" is known but requires an object "error" field.
            "event": {"type": "error"},
        });
        let err = Envelope::from_value(value).unwrap_err();
        assert!(
            matches!(err, EventError::InvalidPayload { ref type_name, .. } if type_name == "error")
        );
    }

    #[test]
    fn unexpected_envelope_field_is_rejected() {
        let value = json!({
            "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "extra": true,
            "event": minimal_known_event(),
        });
        let err = Envelope::from_value(value).unwrap_err();
        assert!(matches!(err, EventError::UnknownField(ref f) if f == "extra"));
    }

    #[test]
    fn nested_subagent_unknown_type_obeys_the_same_ignorable_rule() {
        let nested = json!({"type": "mystery"});
        let subagent = json!({"type": "subagent", "child_run_id": "child-1", "event": nested});

        let strict =
            json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": subagent.clone()});
        assert!(matches!(
            Envelope::from_value(strict).unwrap_err(),
            EventError::UnknownType(ref t) if t == "mystery"
        ));

        let ignorable = json!({
            "v": WIRE_VERSION, "seq": 1, "run_id": "r1", "ignorable": true, "event": subagent,
        });
        let envelope = Envelope::from_value(ignorable.clone()).unwrap();
        assert_eq!(serde_json::to_value(&envelope).unwrap(), ignorable);
    }

    #[test]
    fn subagent_nesting_past_the_limit_is_rejected() {
        let mut event = json!({"type": "error", "error": {}});
        for depth in 0..=MAX_SUBAGENT_DEPTH {
            event = json!({
                "type": "subagent",
                "child_run_id": format!("child-{depth}"),
                "event": event,
            });
        }
        let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": event});
        let err = Envelope::from_value(value).unwrap_err();
        assert!(matches!(err, EventError::NestingTooDeep { max, .. } if max == MAX_SUBAGENT_DEPTH));
    }

    #[test]
    fn subagent_nesting_at_the_limit_is_accepted() {
        let mut event = json!({"type": "error", "error": {}});
        for depth in 0..MAX_SUBAGENT_DEPTH {
            event = json!({
                "type": "subagent",
                "child_run_id": format!("child-{depth}"),
                "event": event,
            });
        }
        let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": event});
        assert!(Envelope::from_value(value).is_ok());
    }

    #[test]
    fn ignorable_flag_is_omitted_when_false() {
        let value =
            json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1", "event": minimal_known_event()});
        let envelope = Envelope::from_value(value.clone()).unwrap();
        assert_eq!(serde_json::to_value(&envelope).unwrap(), value);
    }

    #[test]
    fn from_str_rejects_invalid_json() {
        use std::str::FromStr;
        assert!(Envelope::from_str("{not json").is_err());
    }

    #[test]
    fn missing_v_is_rejected() {
        let value = json!({"seq": 1, "run_id": "r1", "event": minimal_known_event()});
        assert!(matches!(
            Envelope::from_value(value).unwrap_err(),
            EventError::MissingField("v")
        ));
    }

    #[test]
    fn missing_seq_is_rejected() {
        let value = json!({"v": WIRE_VERSION, "run_id": "r1", "event": minimal_known_event()});
        assert!(matches!(
            Envelope::from_value(value).unwrap_err(),
            EventError::MissingField("seq")
        ));
    }

    #[test]
    fn missing_run_id_is_rejected() {
        let value = json!({"v": WIRE_VERSION, "seq": 1, "event": minimal_known_event()});
        assert!(matches!(
            Envelope::from_value(value).unwrap_err(),
            EventError::MissingField("run_id")
        ));
    }

    #[test]
    fn missing_event_is_rejected() {
        let value = json!({"v": WIRE_VERSION, "seq": 1, "run_id": "r1"});
        assert!(matches!(
            Envelope::from_value(value).unwrap_err(),
            EventError::MissingField("event")
        ));
    }

    /// Whether `event`, or any event nested inside it via `subagent`,
    /// contains an [`super::super::IgnorableEvent`] — i.e. whether encoding
    /// it into an envelope requires `ignorable: true` to decode back.
    fn contains_ignorable(event: &Event) -> bool {
        match event {
            Event::Ignorable(_) => true,
            Event::Subagent(inner) => contains_ignorable(&inner.event),
            _ => false,
        }
    }

    proptest::proptest! {
        /// The actual wire boundary the card describes: an arbitrary event,
        /// wrapped in an [`Envelope`], serialized to JSON text, and parsed
        /// back, round-trips byte-for-byte equal — not just `Event::decode`
        /// on a bare value, which `events::event::tests` already covers.
        #[test]
        fn envelope_round_trips_through_json_text(
            seq in proptest::prelude::any::<u64>(),
            run_id in "[a-zA-Z0-9_-]{1,8}",
            event in super::super::arbitrary::event(3),
        ) {
            let envelope = Envelope {
                v: WIRE_VERSION,
                seq,
                run_id: RunId::new(run_id),
                ignorable: contains_ignorable(&event),
                event,
            };
            let text = serde_json::to_string(&envelope).unwrap();
            let decoded: Envelope = text.parse().unwrap();
            proptest::prop_assert_eq!(decoded, envelope);
        }
    }
}
