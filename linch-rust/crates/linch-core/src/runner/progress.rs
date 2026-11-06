//! [`Progress`]: where one call of a run picks it up.

use std::time::Instant;

use crate::run::{Checkpoint, StopReason};
use crate::usage::Usage;

/// What one call of a run (the run, a resume or an approve) starts from:
/// the turn index, usage and last stop reason carried over from durable
/// state, and when the call started.
#[derive(Clone, Copy)]
pub(super) struct Progress {
    pub(super) turn: u32,
    pub(super) usage: Usage,
    pub(super) usage_complete: bool,
    /// Why the last response stopped, for `post_turn`.
    pub(super) stop_reason: StopReason,
    /// When this call started, for the result's `duration_ms`.
    pub(super) started: Instant,
}

impl Progress {
    /// A new run started at `started`: turn 0, nothing spent.
    pub(super) fn first(started: Instant) -> Self {
        Self {
            turn: 0,
            usage: Usage::default(),
            usage_complete: true,
            stop_reason: StopReason::EndTurn,
            started,
        }
    }

    /// A run continued at `started` from `checkpoint`, whose last response
    /// stopped for `stop_reason`.
    pub(super) fn of(checkpoint: &Checkpoint, stop_reason: StopReason, started: Instant) -> Self {
        Self {
            turn: checkpoint.turn,
            usage: checkpoint.usage,
            usage_complete: checkpoint.usage_complete,
            stop_reason,
            started,
        }
    }
}
