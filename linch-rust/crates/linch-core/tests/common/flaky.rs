//! A provider whose calls fail before it answers, for the provider
//! reliability tests.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Mutex;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::ScriptedError;
use linch_core::{
    Provider, ProviderError, ProviderEvent, ProviderRequest, ProviderStream, Role, ScriptedTurn,
};
use tokio_util::sync::CancellationToken;

/// Answers by the request, as [`super::Replay`] does: a request with `n`
/// assistant messages gets `turns[n]`, its `message_start` naming the
/// model the request asked for. Before that, a call fails: with the next
/// of the failures scripted for its turn and model, or always when its
/// model is down. Every call is logged as `(turn, model)`.
pub struct Flaky {
    turns: Vec<ScriptedTurn>,
    failures: Mutex<HashMap<(usize, String), VecDeque<ScriptedTurn>>>,
    down: Mutex<HashSet<String>>,
    error: ScriptedError,
    calls: Mutex<Vec<(usize, String)>>,
    requests: Mutex<Vec<ProviderRequest>>,
}

/// A 503 the provider fails with when a model is down.
pub fn busy() -> ScriptedError {
    ScriptedError::Status {
        status: 503,
        retry_after_ms: None,
        message: "overloaded".into(),
    }
}

impl Flaky {
    pub fn new(turns: Vec<ScriptedTurn>) -> Self {
        Self {
            turns,
            failures: Mutex::default(),
            down: Mutex::default(),
            error: busy(),
            calls: Mutex::default(),
            requests: Mutex::default(),
        }
    }

    /// Fail the next call of `model` in `turn` with `failure`: its events,
    /// then its error (an error with no events fails the call itself).
    pub fn fail(self, turn: usize, model: &str, failure: ScriptedTurn) -> Self {
        let key = (turn, model.to_owned());
        (self.failures.lock().unwrap().entry(key).or_default()).push_back(failure);
        self
    }

    /// Fail the next call of `model` in `turn` with `error`, before any
    /// event.
    pub fn fail_with(self, turn: usize, model: &str, error: ScriptedError) -> Self {
        self.fail(turn, model, ScriptedTurn::failing(error))
    }

    /// Fail every call of `model` with a 503.
    pub fn down(self, model: &str) -> Self {
        self.down.lock().unwrap().insert(model.to_owned());
        self
    }

    /// Every call so far, as `(turn, model)`.
    pub fn calls(&self) -> Vec<(usize, String)> {
        self.calls.lock().unwrap().clone()
    }

    /// Every request so far.
    pub fn requests(&self) -> Vec<ProviderRequest> {
        self.requests.lock().unwrap().clone()
    }

    fn outcome(&self, turn: usize, model: &str) -> ScriptedTurn {
        let key = (turn, model.to_owned());
        if let Some(failure) =
            (self.failures.lock().unwrap().get_mut(&key)).and_then(|q| q.pop_front())
        {
            return failure;
        }
        if self.down.lock().unwrap().contains(model) {
            return ScriptedTurn::failing(self.error.clone());
        }
        self.turns[turn].clone()
    }
}

#[async_trait]
impl Provider for Flaky {
    fn id(&self) -> &str {
        "flaky"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let turn = (request.messages.iter())
            .filter(|message| message.role == Role::Assistant)
            .count();
        let model = request.model.clone();
        self.calls.lock().unwrap().push((turn, model.clone()));
        self.requests.lock().unwrap().push(request);
        let outcome = self.outcome(turn, &model);
        if outcome.events.is_empty()
            && let Some(error) = outcome.error
        {
            return Err(error.into());
        }
        let mut items: Vec<Result<ProviderEvent, ProviderError>> = (outcome.events.into_iter())
            .map(|event| match event {
                ProviderEvent::MessageStart(mut start) => {
                    start.model = model.clone();
                    Ok(ProviderEvent::MessageStart(start))
                }
                event => Ok(event),
            })
            .collect();
        items.extend(outcome.error.map(|error| Err(error.into())));
        Ok(futures::stream::iter(items).boxed())
    }
}
