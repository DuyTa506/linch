//! Provider reliability: a failed provider call is retried on the same
//! model, then the run fails over to the spec's `fallback_models`.
//!
//! [`ProviderRetry`] (the spec's `provider_retry`) bounds the same-model
//! retries and shapes their backoff; [`ProviderRetry::delay`] is the whole
//! rule. Only a [`ProviderError::retryable`] failure is retried or fails
//! over: no response at all, a request timeout, a rate limit or a server
//! error. The runner (`runner/retry.rs`) makes every attempt its own
//! delivery with its own receipt, behind the usual barrier.
//!
//! Nothing new is checkpointed. Where a run stands, which fallback it is
//! on and how many retries the current turn has spent, is read back from
//! its durable `provider_retry` and `model_fallback` events
//! ([`Standing::of`]), so a resumed run neither returns to a model it gave
//! up on nor starts the turn's retries over.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::events::{Envelope, Event};
use crate::provider::ProviderError;

fn default_max_attempts() -> u32 {
    5
}

fn default_base_delay_ms() -> u64 {
    1_000
}

fn default_max_delay_ms() -> u64 {
    30_000
}

fn default_jitter() -> f64 {
    0.2
}

/// How a provider call that failed with a retryable error is retried on the
/// same model. On by default in every [`AgentSpec`](crate::AgentSpec), with
/// Python's `RetryOptions`; `max_attempts: 1` turns it off.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRetry {
    /// Most calls to one model in one turn, the first included (default 5);
    /// 1 never retries.
    #[serde(default = "default_max_attempts")]
    pub max_attempts: u32,
    /// Wait before the first retry, in milliseconds (default 1000); each
    /// later retry waits twice as long as the one before.
    #[serde(default = "default_base_delay_ms")]
    pub base_delay_ms: u64,
    /// Longest wait, in milliseconds (default 30000). It caps the doubling,
    /// and a server's `Retry-After` longer than it ends the retries on that
    /// model rather than hold the run.
    #[serde(default = "default_max_delay_ms")]
    pub max_delay_ms: u64,
    /// Fraction of each doubled wait added or taken away (default 0.2), so
    /// runs that failed together do not call again together. 0 waits
    /// exactly; a `Retry-After` is never jittered.
    #[serde(default = "default_jitter")]
    pub jitter: f64,
}

impl Default for ProviderRetry {
    fn default() -> Self {
        Self {
            max_attempts: default_max_attempts(),
            base_delay_ms: default_base_delay_ms(),
            max_delay_ms: default_max_delay_ms(),
            jitter: default_jitter(),
        }
    }
}

impl ProviderRetry {
    /// Whether this is the default policy, which a spec leaves off the wire.
    pub fn is_default(&self) -> bool {
        *self == Self::default()
    }

    /// Why this policy cannot run, if it cannot.
    pub(crate) fn problem(&self) -> Option<&'static str> {
        if self.max_attempts == 0 {
            return Some("provider_retry.max_attempts must be at least 1");
        }
        if !(0.0..=1.0).contains(&self.jitter) {
            return Some("provider_retry.jitter must be between 0 and 1");
        }
        None
    }

    /// The wait before retry `retry` (from 1) of a call that failed with
    /// `error`; `None` when the model gets no more calls this turn.
    ///
    /// A non-retryable error, or a model that already had `max_attempts`
    /// calls, gets none. A `Retry-After` is waited as given, and one longer
    /// than `max_delay_ms` gets none. Otherwise the wait is `base_delay_ms`
    /// doubled for each earlier retry, capped at `max_delay_ms`, then moved
    /// by up to `jitter` of itself, by an amount drawn from `seed` (the
    /// failed delivery's id), never past `max_delay_ms`.
    ///
    /// Args:
    ///     retry: Which retry of this model in this turn, from 1.
    ///     error: How the last call failed.
    ///     seed: Identifies the failed call; the jitter is drawn from it.
    ///
    /// Returns:
    ///     How long to wait before calling the model again, or `None`.
    pub fn delay(&self, retry: u32, error: &ProviderError, seed: &str) -> Option<Duration> {
        if retry == 0 || retry >= self.max_attempts || !error.retryable() {
            return None;
        }
        let max = Duration::from_millis(self.max_delay_ms);
        if let ProviderError::Status {
            retry_after: Some(wait),
            ..
        } = error
        {
            // Never shorten a server-requested wait, and never hold the run
            // for an unbounded server-chosen time.
            return (*wait <= max).then_some(*wait);
        }
        let doubled = (self.base_delay_ms)
            .saturating_mul(1u64.checked_shl(retry - 1).unwrap_or(u64::MAX))
            .min(self.max_delay_ms) as f64;
        let moved = doubled * (1.0 + self.jitter * spread(seed));
        Some(Duration::from_millis(moved.round() as u64).min(max))
    }
}

/// A number in `[-1, 1]` drawn from `seed`.
fn spread(seed: &str) -> f64 {
    let hash = Sha256::digest(seed.as_bytes());
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&hash[..8]);
    let unit = u64::from_le_bytes(bytes) as f64 / u64::MAX as f64;
    unit * 2.0 - 1.0
}

/// Where a run stands on its fallback chain, and in its current turn.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct Standing {
    /// How many of the spec's `fallback_models` the run has given up on
    /// the model before: 0 calls the spec's model, `n` its `n`th fallback.
    pub(crate) fallbacks: usize,
    /// Retries the current turn has spent on its current model.
    pub(crate) retries: u32,
    /// Whether the current turn failed over, so that its calls go to the
    /// fallback even where a loaded skill names a model.
    pub(crate) failed_over: bool,
}

impl Standing {
    /// Where a run with these durable `events` stands: the fallback its
    /// last `model_fallback` event chose (by its place in
    /// `fallback_models`), and the `provider_retry` events since the
    /// current turn began on that model. An `assistant` event ends a turn.
    pub(crate) fn of(events: &[Envelope], fallback_models: &[String]) -> Self {
        let mut standing = Self::default();
        for envelope in events {
            match &envelope.event {
                Event::Assistant(_) => {
                    standing.retries = 0;
                    standing.failed_over = false;
                }
                Event::ProviderRetry(_) => standing.retries += 1,
                Event::ModelFallback(fallback) => {
                    let chosen = fallback_models.iter().position(|m| *m == fallback.to_model);
                    standing.fallbacks = chosen.map_or(standing.fallbacks, |at| at + 1);
                    standing.retries = 0;
                    standing.failed_over = true;
                }
                _ => {}
            }
        }
        standing
    }

    /// The model the run calls now: the spec's `model` until it fails
    /// over, then the fallback it chose.
    pub(crate) fn model<'a>(&self, model: &'a str, fallback_models: &'a [String]) -> &'a str {
        match self.fallbacks.checked_sub(1) {
            None => model,
            Some(at) => fallback_models.get(at).map_or(model, String::as_str),
        }
    }
}

/// Why `fallback_models` cannot run with `model`, if it cannot.
pub(crate) fn fallback_problem(model: &str, fallback_models: &[String]) -> Option<&'static str> {
    for (at, fallback) in fallback_models.iter().enumerate() {
        if fallback.trim().is_empty() {
            return Some("a fallback model must not be blank");
        }
        if fallback == model || fallback_models[..at].contains(fallback) {
            return Some("fallback_models must differ from the model and from each other");
        }
    }
    None
}

#[cfg(test)]
mod tests;
