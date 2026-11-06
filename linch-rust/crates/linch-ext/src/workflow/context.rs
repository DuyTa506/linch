//! [`WorkflowContext`]: the handle a workflow function orchestrates with.

use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use linch_core::JsonValue;
use linch_core::events::kinds::{WorkflowEvent, WorkflowKind};
use serde::Serialize;
use serde::de::DeserializeOwned;

use super::error::{BoxError, WorkflowError};
use super::journal::{JournalLease, JournalRecord, JournalStore, step_key};

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
}

/// The orchestration handle passed to a workflow function.
///
/// Cheap to clone; clones share one journal. Only journaled calls
/// ([`step`](Self::step)) replay on resume: everything else in the
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
        store: Arc<dyn JournalStore>,
        lease: JournalLease,
        records: Vec<JournalRecord>,
        on_event: Option<EventSink>,
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
            store,
            lease,
            journal: Mutex::new(journal),
            on_event,
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
    ///     options: The step's input key.
    ///     f: The work; called again for each attempt.
    ///
    /// Returns:
    ///     `f`'s value, decoded from its journaled JSON.
    ///
    /// Raises:
    ///     WorkflowError: `Step` when `f` fails, `Encode` when its value
    ///         cannot round-trip through JSON, `Store` when the journal
    ///         write fails or the lease was lost.
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
        emit(WorkflowKind::StepStart, None);
        let output = f().await.map_err(|error| WorkflowError::Step {
            name: name.to_owned(),
            source: error.into(),
        })?;
        let (value, output) = round_trip(&label, &output)?;
        self.record(&call_key, occurrence, &value).await?;
        emit(WorkflowKind::StepEnd, Some(&value));
        Ok(output)
    }

    /// This call's occurrence of `call_key`, and its journaled value when
    /// a previous run recorded one.
    fn next_occurrence(&self, call_key: &str) -> (u64, Option<JsonValue>) {
        let mut journal = self.journal();
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
        self.journal().records.insert(key, value.clone());
        Ok(())
    }

    fn emit(&self, event: WorkflowEvent) {
        if let Some(sink) = &self.inner.on_event {
            sink(&event);
        }
    }

    fn journal(&self) -> MutexGuard<'_, Journal> {
        // Each update is one map write, so a poisoned lock holds no
        // half-applied change.
        (self.inner.journal.lock()).unwrap_or_else(PoisonError::into_inner)
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
