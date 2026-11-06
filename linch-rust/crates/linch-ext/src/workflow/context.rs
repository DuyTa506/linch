//! [`WorkflowContext`]: the handle a workflow function orchestrates with.

use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use linch_core::events::kinds::{WorkflowEvent, WorkflowKind};
use linch_core::{JsonObject, JsonValue};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;

use super::engine::WorkflowOptions;
use super::error::{BoxError, WorkflowError};
use super::journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, interrupt_key, step_key,
};
use super::policy::{Retry, attempt_timeout, run_attempts};

/// Receives every [`WorkflowEvent`] a workflow emits, in order.
pub type EventSink = Arc<dyn Fn(&WorkflowEvent) + Send + Sync>;

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

/// The orchestration handle passed to a workflow function.
///
/// Cheap to clone; clones share one journal. Only journaled calls
/// ([`step`](Self::step), [`interrupt`](Self::interrupt)) replay on
/// resume: everything else in the
/// workflow function runs again, so its control flow must not depend on
/// anything the journal never saw (randomness, the clock, the
/// environment). Put such values inside a step.
#[derive(Clone)]
pub struct WorkflowContext {
    inner: Arc<Inner>,
}

struct Inner {
    store: Arc<dyn JournalStore>,
    lease: JournalLease,
    journal: Mutex<Journal>,
    on_event: Option<EventSink>,
    step_timeout: Option<Duration>,
    cancel: CancellationToken,
    /// Answers not used yet, by interrupt key; each is used once.
    resume: Mutex<HashMap<String, JsonValue>>,
    /// The first interrupt that suspended this run.
    suspended: Mutex<Option<Interrupt>>,
}

/// Journaled results by `(call_key, occurrence)`, and how many calls of
/// each key this run has made.
#[derive(Default)]
struct Journal {
    records: HashMap<(String, u64), JsonValue>,
    counters: HashMap<String, u64>,
}

impl WorkflowContext {
    pub(super) fn new(
        lease: JournalLease,
        records: Vec<JournalRecord>,
        options: &WorkflowOptions,
    ) -> Self {
        let records = records
            .into_iter()
            .map(|record| ((record.call_key, record.occurrence), record.value))
            .collect();
        let journal = Journal {
            records,
            counters: HashMap::new(),
        };
        let inner = Inner {
            store: options.journal.clone(),
            lease,
            journal: Mutex::new(journal),
            on_event: options.on_event.clone(),
            step_timeout: options.step_timeout,
            cancel: options.cancel.clone(),
            resume: Mutex::new(options.resume.clone()),
            suspended: Mutex::new(None),
        };
        Self {
            inner: Arc::new(inner),
        }
    }

    /// Emit a `phase` event: a progress marker for observers.
    pub fn phase(&self, title: impl Into<String>) {
        self.emit(event(WorkflowKind::Phase, title.into(), String::new(), 0));
    }

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

    /// Park the workflow until someone answers `key`.
    ///
    /// The first time through there is no answer: an `interrupt_requested`
    /// event carries `payload`, and the call returns
    /// [`WorkflowError::Suspended`]; [`run_workflow`](super::run_workflow)
    /// then stores the workflow as awaiting input, with the interrupt, so
    /// any process can read the question. Calling `run_workflow` again
    /// with the answer in [`WorkflowOptions::resume`] makes this call
    /// return it; the answer is journaled, so later resumes replay it
    /// without a `resume` entry.
    ///
    /// An answer is used once: a second interrupt with the same key in one
    /// run suspends again, so interrupts in a loop need distinct keys.
    ///
    /// Args:
    ///     key: The interrupt's durable identity and the answer's key.
    ///     payload: Context for whoever answers; it does not affect replay.
    ///
    /// Returns:
    ///     The answer, decoded from its journaled JSON.
    ///
    /// Raises:
    ///     WorkflowError: `Suspended` without an answer, `Aborted` when
    ///         the cancel token fired, `Encode` when the payload does not
    ///         encode or the answer does not decode, `Store` when the
    ///         journal write fails.
    pub async fn interrupt<A: DeserializeOwned>(
        &self,
        key: &str,
        payload: impl Serialize,
    ) -> Result<A, WorkflowError> {
        let label = format!("interrupt {key}");
        let call_key = interrupt_key(key);
        let (occurrence, cached) = self.next_occurrence(&call_key);
        let event = |kind, result_text: Option<&JsonValue>| {
            let mut event = event(kind, key.to_owned(), call_key.clone(), occurrence);
            event.result_text = result_text.map(JsonValue::to_string);
            event
        };
        if let Some(answer) = cached {
            self.emit(event(WorkflowKind::InterruptReplayed, Some(&answer)));
            return decode(&label, answer);
        }
        self.check_cancel(&label)?;
        let answer = self.lock(&self.inner.resume).remove(key);
        if let Some(answer) = answer {
            let decoded = decode(&label, answer.clone())?;
            self.record(&call_key, occurrence, &answer).await?;
            self.emit(event(WorkflowKind::InterruptResolved, Some(&answer)));
            return Ok(decoded);
        }
        let payload = serde_json::to_value(payload).map_err(|error| {
            encode_error(&label, format!("its payload does not encode: {error}"))
        })?;
        let mut requested = event(WorkflowKind::InterruptRequested, None);
        let mut structured = JsonObject::new();
        structured.insert("payload".into(), payload.clone());
        requested.structured_output = Some(structured);
        self.emit(requested);
        let interrupt = Interrupt {
            key: key.to_owned(),
            payload,
        };
        self.lock(&self.inner.suspended)
            .get_or_insert_with(|| interrupt.clone());
        Err(WorkflowError::Suspended(interrupt))
    }

    /// The first interrupt that suspended this run, if any did.
    pub(super) fn suspended(&self) -> Option<Interrupt> {
        self.lock(&self.inner.suspended).clone()
    }

    /// This call's occurrence of `call_key`, and its journaled value when
    /// a previous run recorded one.
    fn next_occurrence(&self, call_key: &str) -> (u64, Option<JsonValue>) {
        let mut journal = self.lock(&self.inner.journal);
        let counter = journal.counters.entry(call_key.to_owned()).or_default();
        let occurrence = *counter;
        *counter += 1;
        let cached = journal.records.get(&(call_key.to_owned(), occurrence));
        (occurrence, cached.cloned())
    }

    /// Make one result durable, then cache it.
    async fn record(
        &self,
        call_key: &str,
        occurrence: u64,
        value: &JsonValue,
    ) -> Result<(), WorkflowError> {
        let record = JournalRecord {
            call_key: call_key.to_owned(),
            occurrence,
            value: value.clone(),
        };
        self.inner.store.append(&self.inner.lease, record).await?;
        let key = (call_key.to_owned(), occurrence);
        self.lock(&self.inner.journal)
            .records
            .insert(key, value.clone());
        Ok(())
    }

    /// `Aborted` once the workflow's cancel token fired: no new work starts,
    /// while replays still return.
    fn check_cancel(&self, label: &str) -> Result<(), WorkflowError> {
        if self.inner.cancel.is_cancelled() {
            return Err(WorkflowError::Aborted {
                label: label.to_owned(),
            });
        }
        Ok(())
    }

    fn emit(&self, event: WorkflowEvent) {
        if let Some(sink) = &self.inner.on_event {
            sink(&event);
        }
    }

    fn lock<'a, T>(&self, mutex: &'a Mutex<T>) -> MutexGuard<'a, T> {
        // Each update is one map or option write, so a poisoned lock holds
        // no half-applied change.
        mutex.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

fn event(kind: WorkflowKind, title: String, call_key: String, occurrence: u64) -> WorkflowEvent {
    WorkflowEvent {
        kind,
        title,
        call_key,
        occurrence,
        subagent_type: String::new(),
        result_text: None,
        structured_output: None,
        structured_error: None,
    }
}

fn encode_error(label: &str, message: String) -> WorkflowError {
    WorkflowError::Encode {
        label: label.to_owned(),
        message,
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

fn decode<T: DeserializeOwned>(label: &str, value: JsonValue) -> Result<T, WorkflowError> {
    serde_json::from_value(value)
        .map_err(|error| encode_error(label, format!("its value does not decode: {error}")))
}
