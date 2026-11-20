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
    /// told apart by call order only, counted apart in each `parallel` or
    /// `settled` branch. Objects are compared in canonical form; array
    /// order counts.
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
        let content_key = step_key(name, options.key.as_ref())
            .map_err(|error| encode_error(&label, format!("its key does not encode: {error}")))?;
        let (call_key, occurrence, cached) = self.next_occurrence(&content_key)?;
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

/// A step that has to run: handed out by [`WorkflowContext::step_begin`]
/// and given back to [`WorkflowContext::step_complete`] with the value.
#[derive(Debug)]
pub struct StepTicket {
    name: String,
    call_key: String,
    occurrence: u64,
}

/// What [`WorkflowContext::step_begin`] found in the journal.
#[derive(Debug)]
pub enum StepSlot {
    /// An earlier run finished this step: its value, to use as is.
    Replayed(JsonValue),
    /// The step has no record: run it, then complete the ticket.
    Run(StepTicket),
}

impl WorkflowContext {
    /// Start a journaled step whose function runs elsewhere (a client of
    /// the RPC server): the first half of [`step_with`](Self::step_with).
    ///
    /// Returns the journaled value when an earlier run finished the step
    /// (and emits `step_replayed`), else a ticket (and emits
    /// `step_start`). The occurrence is counted here, in call order, so a
    /// step that is begun and never completed still uses its place.
    ///
    /// Args:
    ///     name: The step's durable identity, as in `step_with`.
    ///     key: The identity of the step's inputs, as in
    ///         [`StepOptions::key`].
    ///
    /// Raises:
    ///     WorkflowError: `Encode` when `key` does not encode, `Aborted`
    ///         when the cancel token fired first.
    pub fn step_begin(
        &self,
        name: &str,
        key: Option<&JsonValue>,
    ) -> Result<StepSlot, WorkflowError> {
        let label = format!("step {name}");
        let content_key = step_key(name, key)
            .map_err(|error| encode_error(&label, format!("its key does not encode: {error}")))?;
        let (call_key, occurrence, cached) = self.next_occurrence(&content_key)?;
        let mut event = event(
            WorkflowKind::StepReplayed,
            name.to_owned(),
            call_key.clone(),
            occurrence,
        );
        if let Some(value) = cached {
            event.result_text = Some(value.to_string());
            self.emit(event);
            return Ok(StepSlot::Replayed(value));
        }
        self.check_cancel(&label)?;
        self.check_room()?;
        event.kind = WorkflowKind::StepStart;
        self.emit(event);
        Ok(StepSlot::Run(StepTicket {
            name: name.to_owned(),
            call_key,
            occurrence,
        }))
    }

    /// Journal the value of a step started with
    /// [`step_begin`](Self::step_begin); it is durable when this returns.
    ///
    /// Raises:
    ///     WorkflowError: `Store` when the journal write fails or the lease
    ///         was lost, `LimitExceeded` past the record cap.
    pub async fn step_complete(
        &self,
        ticket: StepTicket,
        value: JsonValue,
    ) -> Result<(), WorkflowError> {
        self.record(&ticket.call_key, ticket.occurrence, &value)
            .await?;
        let mut event = event(
            WorkflowKind::StepEnd,
            ticket.name,
            ticket.call_key,
            ticket.occurrence,
        );
        event.result_text = Some(value.to_string());
        self.emit(event);
        Ok(())
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
