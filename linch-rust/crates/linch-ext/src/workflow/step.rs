//! Journaled steps: host code that a resume replays instead of running
//! again.

use std::future::Future;
use std::time::Duration;

use linch_core::JsonValue;
use linch_core::events::kinds::WorkflowKind;
use serde::Serialize;
use serde::de::DeserializeOwned;

use super::context::{WorkflowContext, decode, encode_error, event};
use super::error::{BoxError, WorkflowError};
use super::journal::step_key;
use super::policy::{Retry, attempt_timeout, run_attempts};

/// Options for one [`WorkflowContext::step_with`] call.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StepOptions {
    /// Identity of the step's inputs, so one step name over different
    /// inputs is journaled separately. Without it, calls of one name are
    /// told apart by call order only. Objects are compared in canonical
    /// form; array order counts.
    pub key: Option<JsonValue>,
    /// Limit on each attempt; `None` uses the workflow's `step_timeout`,
    /// and zero opts out of it. An attempt past it fails with
    /// [`WorkflowError::Timeout`].
    pub timeout: Option<Duration>,
    /// Retries after a failed attempt; `None` is one attempt.
    pub retry: Option<Retry>,
}

impl WorkflowContext {
    /// Run `f` as a journaled step named `name` and return its value.
    ///
    /// The same as [`step_with`](Self::step_with) with default options.
    pub async fn step<T, E, F, Fut>(&self, name: &str, f: F) -> Result<T, WorkflowError>
    where
        T: Serialize + DeserializeOwned,
        E: Into<BoxError>,
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T, E>>,
    {
        self.step_with(name, &StepOptions::default(), f).await
    }

    /// Run `f` as a journaled step and return its value.
    ///
    /// This is how host code with side effects becomes a node that a
    /// resume replays instead of running again. The value goes through
    /// JSON on every path, so a replayed value equals the live one; it is
    /// durable in the journal before this returns. A process that crashes
    /// after `f` ran but before the record landed runs `f` again on
    /// resume, so side effects should be idempotent.
    ///
    /// Args:
    ///     name: The step's durable identity; renaming it makes its old
    ///         record miss, while later steps whose keys still match replay.
    ///     options: The step's input key, timeout and retries.
    ///     f: The work; called again for each attempt.
    ///
    /// Returns:
    ///     `f`'s value, decoded from its journaled JSON.
    ///
    /// Raises:
    ///     WorkflowError: `Step` when the last attempt fails, `Timeout`
    ///         when it runs past its limit, `Aborted` when the cancel token
    ///         fired first, `Encode` when the value cannot round-trip
    ///         through JSON, `Store` when the journal write fails or the
    ///         lease was lost.
    pub async fn step_with<T, E, F, Fut>(
        &self,
        name: &str,
        options: &StepOptions,
        mut f: F,
    ) -> Result<T, WorkflowError>
    where
        T: Serialize + DeserializeOwned,
        E: Into<BoxError>,
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T, E>>,
    {
        let label = format!("step {name}");
        let call_key = step_key(name, options.key.as_ref())
            .map_err(|error| encode_error(&label, format!("its key does not encode: {error}")))?;
        let (occurrence, cached) = self.next_occurrence(&call_key);
        let emit = |kind, result_text: Option<&JsonValue>| {
            let mut event = event(kind, name.to_owned(), call_key.clone(), occurrence);
            event.result_text = result_text.map(JsonValue::to_string);
            self.emit(event);
        };
        if let Some(value) = cached {
            emit(WorkflowKind::StepReplayed, Some(&value));
            return decode(&label, value);
        }
        self.check_cancel(&label)?;
        emit(WorkflowKind::StepStart, None);
        let timeout = attempt_timeout(options.timeout, self.inner.step_timeout);
        let retry = options.retry.as_ref();
        let label = &label;
        let (value, output) = run_attempts(label, timeout, retry, &self.inner.cancel, || {
            let attempt = f();
            async move {
                let output = attempt.await.map_err(|error| WorkflowError::Step {
                    name: name.to_owned(),
                    source: error.into(),
                })?;
                round_trip(label, &output)
            }
        })
        .await?;
        self.record(&call_key, occurrence, &value).await?;
        emit(WorkflowKind::StepEnd, Some(&value));
        Ok(output)
    }
}

/// `value` as JSON, and decoded back, so the caller gets exactly what a
/// replay would give.
fn round_trip<T: Serialize + DeserializeOwned>(
    label: &str,
    value: &T,
) -> Result<(JsonValue, T), WorkflowError> {
    let json = serde_json::to_value(value)
        .map_err(|error| encode_error(label, format!("its value does not encode: {error}")))?;
    let decoded = decode(label, json.clone())?;
    Ok((json, decoded))
}
