//! [`WorkflowContext`]: the handle a workflow function orchestrates with,
//! and the journal plumbing its calls share. The calls themselves live in
//! `step`, `interrupt` and `fanout`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use linch_core::JsonValue;
use linch_core::events::kinds::{WorkflowEvent, WorkflowKind};
use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;

use super::engine::WorkflowOptions;
use super::error::WorkflowError;
use super::journal::{Interrupt, JournalLease, JournalRecord, JournalStore};

/// Receives every [`WorkflowEvent`] a workflow emits, in order.
pub type EventSink = Arc<dyn Fn(&WorkflowEvent) + Send + Sync>;

/// The orchestration handle passed to a workflow function.
///
/// Cheap to clone; clones share one journal. Only journaled calls
/// ([`step`](Self::step), [`interrupt`](Self::interrupt)) replay on
/// resume: everything else in the workflow function runs again, so its
/// control flow must not depend on anything the journal never saw
/// (randomness, the clock, the environment). Put such values inside a
/// step.
#[derive(Clone)]
pub struct WorkflowContext {
    pub(super) inner: Arc<Inner>,
}

pub(super) struct Inner {
    store: Arc<dyn JournalStore>,
    pub(super) lease: JournalLease,
    journal: Mutex<Journal>,
    on_event: Option<EventSink>,
    pub(super) step_timeout: Option<Duration>,
    pub(super) cancel: CancellationToken,
    pub(super) max_concurrency: usize,
    /// Answers not used yet, by interrupt key; each is used once.
    pub(super) resume: Mutex<HashMap<String, JsonValue>>,
    /// The first interrupt that suspended this run.
    pub(super) suspended: Mutex<Option<Interrupt>>,
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
            max_concurrency: options.max_concurrency.max(1),
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

    /// The first interrupt that suspended this run, if any did.
    pub(super) fn suspended(&self) -> Option<Interrupt> {
        lock(&self.inner.suspended).clone()
    }

    /// This call's occurrence of `call_key`, and its journaled value when
    /// a previous run recorded one.
    pub(super) fn next_occurrence(&self, call_key: &str) -> (u64, Option<JsonValue>) {
        let mut journal = lock(&self.inner.journal);
        let counter = journal.counters.entry(call_key.to_owned()).or_default();
        let occurrence = *counter;
        *counter += 1;
        let cached = journal.records.get(&(call_key.to_owned(), occurrence));
        (occurrence, cached.cloned())
    }

    /// Make one result durable, then cache it.
    pub(super) async fn record(
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
        lock(&self.inner.journal).records.insert(key, value.clone());
        Ok(())
    }

    /// `Aborted` once the workflow's cancel token fired: no new work starts,
    /// while replays still return.
    pub(super) fn check_cancel(&self, label: &str) -> Result<(), WorkflowError> {
        if self.inner.cancel.is_cancelled() {
            return Err(WorkflowError::Aborted {
                label: label.to_owned(),
            });
        }
        Ok(())
    }

    pub(super) fn emit(&self, event: WorkflowEvent) {
        if let Some(sink) = &self.inner.on_event {
            sink(&event);
        }
    }
}

pub(super) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // Each update is one map or option write, so a poisoned lock holds no
    // half-applied change.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A workflow event with no result.
pub(super) fn event(
    kind: WorkflowKind,
    title: String,
    call_key: String,
    occurrence: u64,
) -> WorkflowEvent {
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

pub(super) fn encode_error(label: &str, message: String) -> WorkflowError {
    WorkflowError::Encode {
        label: label.to_owned(),
        message,
    }
}

pub(super) fn decode<T: DeserializeOwned>(
    label: &str,
    value: JsonValue,
) -> Result<T, WorkflowError> {
    serde_json::from_value(value)
        .map_err(|error| encode_error(label, format!("its value does not decode: {error}")))
}
