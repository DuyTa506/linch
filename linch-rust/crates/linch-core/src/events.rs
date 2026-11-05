//! The event vocabulary emitted by a run: one payload type per kind,
//! wrapped in a versioned [`Envelope`] for the wire.
//!
//! - [`Event`] is the sum type; see its module (`events/event/mod.rs`) for
//!   why it hand-writes `Serialize`/`Deserialize` instead of deriving them.
//! - [`Envelope`] carries the wire version, a monotonic `seq`, the owning
//!   run, and the producer-declared `ignorable` compatibility flag.
//! - [`EventError`] is the one typed error for every decode failure in this
//!   module.
//! - [`kinds`] and [`model_call`] hold the per-kind payload structs/enums;
//!   most callers only need the re-exports here.
//!
//! Out of scope here: hook points/dispatch (P3), durable storage (P7), and
//! RPC framing (P11) — this module only defines the wire vocabulary.

#[cfg(test)]
mod arbitrary;
mod envelope;
mod event;
pub mod kinds;
pub mod model_call;
mod wire;

pub use envelope::{Envelope, EventError, WIRE_VERSION};
pub use event::{Event, IgnorableEvent, Persistence, SubagentEvent};
