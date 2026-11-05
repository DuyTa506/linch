//! Payload structs for every event kind except the model-call journal
//! records (see [`super::model_call`]) and [`super::IgnorableEvent`].
//!
//! Each type here is a plain, independently `Serialize`/`Deserialize`
//! struct or enum; [`super::Event`] injects and strips the wire `type` tag
//! around them by hand (see `events/event/mod.rs`), so none of these carry
//! a `type` field themselves. Split by topic to stay near this crate's
//! per-file line target; everything is re-exported flat here, so
//! `super::kinds::X` resolves regardless of which file defines `X`.

mod control;
mod lifecycle;
mod skills;
mod suspend;
mod tooling;

pub use control::*;
pub use lifecycle::*;
pub use skills::*;
pub use suspend::*;
pub use tooling::*;

/// Shared `skip_serializing_if` helper: omit a count field when it is zero,
/// matching this crate's "empty/false/zero defaults may be omitted" wire
/// rule (`BLUEPRINT.md` 9.1).
pub(super) fn is_zero(value: &u64) -> bool {
    *value == 0
}
