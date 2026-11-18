//! Provider reliability kinds: a failed call retried on the same model,
//! and the run failing over to a fallback model (see
//! [`crate::reliability`]).

use crate::json::JsonObject;
use serde::{Deserialize, Serialize};

/// A provider call failed with a retryable error and the run calls the
/// same model again after `delay_ms`. The streamed deltas of the failed
/// call are void: the next call answers from the start.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRetryEvent {
    /// The model whose call failed and is called again.
    pub model: String,
    /// Which retry of this model in this turn, from 1.
    pub retry: u32,
    /// How long the run waits before the next call, in milliseconds.
    pub delay_ms: u64,
    /// The failure, shaped as an `error` event's payload: `name`, `kind`,
    /// `message`, `retryable`, and `status` for an HTTP status.
    pub error: JsonObject,
}

/// The run gave up on `from_model` after a retryable failure and calls
/// `to_model` from now on, for the rest of the run (Python's
/// `model_fallback`, with the failure as a structured `error` in place of
/// its `reason` text).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelFallbackEvent {
    /// The model whose last call failed.
    pub from_model: String,
    /// The fallback model the run calls next.
    pub to_model: String,
    /// The failure that ended `from_model`'s calls, shaped as an `error`
    /// event's payload. As for [`ProviderRetryEvent`], the deltas that call
    /// streamed are void: `to_model` answers from the start.
    pub error: JsonObject,
}
