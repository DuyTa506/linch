//! [`Progress`]: where one call of a run picks it up.

use std::time::Instant;

use std::collections::BTreeMap;

use crate::reliability::Standing;
use crate::run::{Checkpoint, StopReason};
use crate::usage::Usage;

/// What one call of a run (the run, a resume or an approve) starts from:
/// the turn index, usage, cost and last stop reason carried over from
/// durable state, and when the call started.
#[derive(Clone)]
pub(super) struct Progress {
    pub(super) turn: u32,
    pub(super) usage: Usage,
    pub(super) usage_complete: bool,
    pub(super) usage_by_model: BTreeMap<String, Usage>,
    /// Cost of the priced calls so far; `None` until one was priced.
    pub(super) cost: Option<f64>,
    /// Why the last response stopped, for `post_turn`.
    pub(super) stop_reason: StopReason,
    /// When this call started, for the result's `duration_ms`.
    pub(super) started: Instant,
    /// Where the run stands on its fallback models and in its turn's
    /// retries.
    pub(super) standing: Standing,
}

impl Progress {
    /// A new run started at `started`: turn 0, nothing spent.
    pub(super) fn first(started: Instant) -> Self {
        Self {
            turn: 0,
            usage: Usage::default(),
            usage_complete: true,
            usage_by_model: BTreeMap::new(),
            cost: None,
            stop_reason: StopReason::EndTurn,
            started,
            standing: Standing::default(),
        }
    }

    /// A run continued at `started` from `checkpoint`, whose last response
    /// stopped for `stop_reason`, standing where its events left it.
    pub(super) fn of(
        checkpoint: &Checkpoint,
        stop_reason: StopReason,
        standing: Standing,
        started: Instant,
    ) -> Self {
        Self {
            turn: checkpoint.turn,
            usage: checkpoint.usage,
            usage_complete: checkpoint.usage_complete,
            usage_by_model: checkpoint.usage_by_model.clone(),
            cost: checkpoint.cost_usd,
            stop_reason,
            started,
            standing,
        }
    }
}
