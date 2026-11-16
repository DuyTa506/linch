//! A failed provider call, retried on the same model or failed over to the
//! next fallback model (see [`crate::reliability`]).
//!
//! Every attempt is a dispatch of its own: [`Step::Dispatch`] with the next
//! attempt number builds the request again, stages its own delivery and
//! flushes it with `ProviderPending` before the call, as a resumed run's
//! redispatch does. What differs is what happens between the attempts:
//!
//! - **A retry** stages a `provider_retry` event after the failed receipt
//!   and its usage, and writes them with `ProviderPending` re-saved at the
//!   totals that count them, before the wait. A crash during the wait
//!   leaves that state: the resumed run sees the failed receipt, dispatches
//!   the next attempt at once, and reads the turn's spent retries back from
//!   the `provider_retry` events. A cancel during the wait aborts the run.
//! - **A failover** stages a `model_fallback` event and dispatches the
//!   next attempt at once to the fallback model; the event rides that
//!   dispatch's barrier. A crash before it leaves the run on the old model
//!   with its retries spent, so it fails over again.
//!
//! Neither happens when the budget refuses the next call: the run ends with
//! the provider's error, as it did before retries.

use super::turn::{Runner, Step};
use crate::agent::RunError;
use crate::events::Event;
use crate::events::kinds::{ModelFallbackEvent, ProviderRetryEvent};
use crate::provider::call::CallFailure;
use crate::reliability::Standing;
use crate::run::Phase;

/// What [`Runner::recover`] made of a failed call.
pub(super) enum Recovered {
    /// Call again: this step dispatches the next attempt.
    Call(Step),
    /// A cancel during the wait ended the run.
    Ended,
    /// Neither retried nor failed over: the call's failure ends the run.
    Failed,
}

impl Runner {
    /// What follows call `attempt` of this turn, which asked for `model`
    /// and failed: another call after a wait, a call to the next fallback
    /// model, or neither, and the run fails with `failure` as before.
    pub(super) async fn recover(
        &mut self,
        failure: &CallFailure,
        attempt: u32,
        model: &str,
    ) -> Result<Recovered, RunError> {
        if !failure.error.retryable() || self.refusal().is_some() {
            return Ok(Recovered::Failed);
        }
        let next = Step::Dispatch {
            attempt: attempt + 1,
            abandoned: None,
        };
        let agent = std::sync::Arc::clone(&self.agent);
        if let Some(policy) = &agent.spec.provider_retry {
            let retry = self.standing.retries + 1;
            let seed = format!("{}:primary:{}:{attempt}", self.run.run_id, self.turn);
            if let Some(delay) = policy.delay(retry, &failure.error, &seed) {
                self.standing.retries = retry;
                self.journal.stage(Event::ProviderRetry(ProviderRetryEvent {
                    model: model.to_owned(),
                    retry,
                    delay_ms: u64::try_from(delay.as_millis()).unwrap_or(u64::MAX),
                    error: failure.event.error.clone(),
                }));
                self.save(Phase::ProviderPending { attempt }).await?;
                let ready = self.journal.flush().await?;
                self.event_emit(&ready).await;
                self.outbox.deliver(ready).await;
                let waited = self.cancel.run_until_cancelled(tokio::time::sleep(delay));
                if waited.await.is_none() {
                    self.abort().await?;
                    return Ok(Recovered::Ended);
                }
                return Ok(Recovered::Call(next));
            }
        }
        let Some(to_model) = agent.spec.fallback_models.get(self.standing.fallbacks) else {
            return Ok(Recovered::Failed);
        };
        self.standing = Standing {
            fallbacks: self.standing.fallbacks + 1,
            retries: 0,
            failed_over: true,
        };
        self.journal.stage(Event::ModelFallback(ModelFallbackEvent {
            from_model: model.to_owned(),
            to_model: to_model.clone(),
            error: failure.event.error.clone(),
        }));
        Ok(Recovered::Call(next))
    }

    /// The model the run calls now: the spec's, or the fallback it failed
    /// over to.
    pub(super) fn model(&self) -> &str {
        let spec = &self.agent.spec;
        self.standing.model(&spec.model, &spec.fallback_models)
    }

    /// The model of this turn's call: a loaded skill's (see
    /// [`crate::skills`]) unless the turn failed over, else the run's.
    pub(super) fn call_model(&self, skill_model: Option<String>) -> String {
        match skill_model {
            Some(skill) if !self.standing.failed_over => {
                crate::skills::model_override(&skill, self.model())
            }
            _ => self.model().to_owned(),
        }
    }
}
