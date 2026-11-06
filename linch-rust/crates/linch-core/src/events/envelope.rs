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

/// Every way encoding or decoding an [`Envelope`] or [`Event`] can fail.
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
    /// A known event's payload failed to serialize (for example, a
    /// non-finite monetary `f64`; see `wire::serialize_finite_f64`).
    #[error("failed to encode payload for event type {type_name:?}: {source}")]
    EncodePayload {
        /// The event type whose payload failed to encode.
        type_name: String,
        /// The underlying serialization error.
        #[source]
        source: serde_json::Error,
    },
    /// The envelope's text was not valid JSON at all (a syntax error, as
    /// opposed to [`EventError::InvalidPayload`], which is well-formed JSON
    /// in the wrong shape).
    #[error("invalid JSON: {0}")]
    InvalidJson(#[source] serde_json::Error),
}

/// The durable envelope around one event.
///
/// `seq` is a monotonic sequence number assigned by the producer so a
/// client can resume a stream from a known point; ordering it is the
/// caller's job, but carrying it is mandatory on every envelope. `ignorable`
/// is the producer's promise that an unknown `event.type` is safe to skip
/// (see `Event::decode` and `docs/architecture/events.md`); it does not
/// rescue a malformed *known* event.
///
/// `Serialize` is hand-written (see [`Envelope::to_value`]) rather than
/// derived: setting `ignorable: false` while `event` carries an
/// [`super::IgnorableEvent`] (directly, or nested under a `subagent`) would
/// otherwise encode an envelope that cannot decode itself back. Encoding
/// always sets the wire `ignorable` flag to `true` in that case regardless
/// of this field's value, so every envelope that encodes also decodes.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    /// Wire version this envelope was written with.
    pub v: u32,
    /// Monotonic sequence number, assigned by the producer.
    pub seq: u64,
    /// Run this envelope belongs to.
    pub run_id: RunId,
    /// Whether an unknown `event.type` should decode to
    /// [`super::IgnorableEvent`] instead of failing.
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

    /// Encode this envelope to its JSON wire representation.
    ///
    /// The wire `ignorable` field is `true` whenever `self.ignorable` is, or
    /// `self.event` (recursively through `subagent`) is an
    /// [`super::IgnorableEvent`] — see the type's docs.
    ///
    /// Raises:
    ///     EventError: If `self.event` fails to encode (see
    ///         [`Event::to_value`]).
    pub fn to_value(&self) -> Result<JsonValue, EventError> {
        let mut obj = crate::json::JsonObject::new();
        obj.insert("v".to_string(), JsonValue::from(self.v));
        obj.insert("seq".to_string(), JsonValue::from(self.seq));
        obj.insert(
            "run_id".to_string(),
            JsonValue::String(self.run_id.as_str().to_string()),
        );
        if self.ignorable || self.event.contains_ignorable() {
            obj.insert("ignorable".to_string(), JsonValue::Bool(true));
        }
        obj.insert("event".to_string(), self.event.to_value()?);
        Ok(JsonValue::Object(obj))
    }
}

impl Serialize for Envelope {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        self.to_value()
            .map_err(serde::ser::Error::custom)?
            .serialize(serializer)
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
        let value: JsonValue = serde_json::from_str(text).map_err(EventError::InvalidJson)?;
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
mod tests;
