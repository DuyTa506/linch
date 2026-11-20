//! [`WorkflowContext`]: the handle a workflow function orchestrates with,
//! and the journal plumbing its calls share. The calls themselves live in
//! `step`, `interrupt` and `fanout`.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use linch_core::events::kinds::{WorkflowEvent, WorkflowKind};
use linch_core::{JsonValue, Principal};
use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;

use super::engine::{WorkflowBudget, WorkflowOptions};
use super::error::WorkflowError;
use super::journal::{Interrupt, JournalLease, JournalRecord, JournalStore};

/// Receives every [`WorkflowEvent`] a workflow emits, in order.
pub type EventSink = Arc<dyn Fn(&WorkflowEvent) + Send + Sync>;

/// What a host asks before an agent call runs live: the guard it returns
/// is held until the call ends, and its error fails the call.
pub type LiveGate =
    Arc<dyn Fn() -> Result<Box<dyn std::any::Any + Send + Sync>, WorkflowError> + Send + Sync>;

/// The orchestration handle passed to a workflow function.
///
/// Cheap to clone; clones share one journal. Only journaled calls
/// ([`step`](Self::step), [`interrupt`](Self::interrupt),
/// [`agent`](Self::agent)) replay on
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
    /// Steps begun with [`step_begin`](WorkflowContext::step_begin) and not
    /// completed or dropped yet; each holds a place under `max_records`.
    pub(super) pending_steps: Arc<AtomicU64>,
    on_event: Option<EventSink>,
    pub(super) step_timeout: Option<Duration>,
    pub(super) cancel: CancellationToken,
    pub(super) max_concurrency: usize,
    /// Answers not used yet, by interrupt key; each is used once.
    pub(super) resume: Mutex<HashMap<String, JsonValue>>,
    /// The first interrupt that suspended this run.
    pub(super) suspended: Mutex<Option<Interrupt>>,
    /// The question the workflow was parked on when this run claimed it:
    /// the only one a resume answer may answer, and only once.
    pub(super) awaiting: Mutex<Option<Interrupt>>,
    pub(super) budget: Option<WorkflowBudget>,
    /// Tokens and USD the workflow's agent calls have spent so far.
    pub(super) spent: Mutex<(u64, f64)>,
    /// The model of a finished call that had usage and no cost, so a USD
    /// cap could not count it.
    pub(super) unpriced: Mutex<Option<String>>,
    max_records: Option<u64>,
    max_record_bytes: Option<usize>,
    pub(super) live_gate: Option<LiveGate>,
    /// Who the agent calls' runs act for.
    pub(super) principal: Option<Principal>,
}

/// Journaled results by `(call_key, occurrence)`, and how many calls of
/// each key this run has made. Inside a fan-out branch the key carries the
/// branch path (see [`WorkflowContext::next_occurrence`]).
#[derive(Default)]
struct Journal {
    records: HashMap<(String, u64), JsonValue>,
    counters: HashMap<String, u64>,
}

impl WorkflowContext {
    pub(super) fn new(
        lease: JournalLease,
        records: Vec<JournalRecord>,
        awaiting: Option<Interrupt>,
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
            pending_steps: Arc::default(),
            on_event: options.on_event.clone(),
            step_timeout: options.step_timeout,
            cancel: options.cancel.clone(),
            max_concurrency: options.max_concurrency.max(1),
            resume: Mutex::new(options.resume.clone()),
            suspended: Mutex::new(None),
            awaiting: Mutex::new(awaiting),
            budget: options.budget,
            spent: Mutex::new((0, 0.0)),
            unpriced: Mutex::new(None),
            max_records: options.max_records,
            max_record_bytes: options.max_record_bytes,
            live_gate: options.live_gate.clone(),
            principal: options.principal.clone(),
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
    pub fn suspended(&self) -> Option<Interrupt> {
        lock(&self.inner.suspended).clone()
    }

    /// What the workflow's agent calls have spent so far, replayed ones
    /// included: tokens, and USD.
    pub fn spent(&self) -> (u64, f64) {
        *lock(&self.inner.spent)
    }

    /// Poll `future` as the code of branch path `path`, the way a
    /// `parallel` branch is polled: the calls it makes count their
    /// occurrences apart from every other path's. For a host whose fan-out
    /// runs elsewhere (a client), which names each branch itself; `path` is
    /// the branch index at each level, outermost first, joined by `.`
    /// (`1`, `0.2`), and empty outside every branch.
    pub fn scoped<F: Future>(&self, path: &str, future: F) -> impl Future<Output = F::Output> {
        BRANCH.scope(path.to_owned(), future)
    }

    /// The journal key of a call with content key `call_key`, this call's
    /// occurrence of it, and its journaled value when a previous run
    /// recorded one.
    ///
    /// Outside every fan-out the journal key is `call_key`. Inside a
    /// `parallel` or `settled` branch it is `call_key@<path>`, the path
    /// being the branch index at each level, outermost first (`1`, `0.2`),
    /// so each branch counts its own calls. Branches reach their calls in a
    /// different order on replay, where journaled calls return at once; a
    /// count shared across branches would hand one branch another's
    /// record.
    ///
    /// With `max_records` set, a call whose key this run has not used and
    /// no record answers is refused once the run holds that many keys, so
    /// calls that journal nothing (a suspended interrupt, a failed call)
    /// cannot grow the counters without bound.
    pub(super) fn next_occurrence(
        &self,
        call_key: &str,
    ) -> Result<(String, u64, Option<JsonValue>), WorkflowError> {
        let path = branch_path();
        let key = if path.is_empty() {
            call_key.to_owned()
        } else {
            format!("{call_key}@{path}")
        };
        let mut journal = lock(&self.inner.journal);
        let seen = journal.counters.get(&key).copied();
        let occurrence = seen.unwrap_or_default();
        let cached = journal.records.get(&(key.clone(), occurrence)).cloned();
        if let Some(max) = self.inner.max_records {
            if seen.is_none() && cached.is_none() && journal.counters.len() as u64 >= max {
                return Err(WorkflowError::LimitExceeded {
                    what: "journal records",
                    max,
                });
            }
        }
        journal.counters.insert(key.clone(), occurrence + 1);
        Ok((key, occurrence, cached))
    }

    /// `LimitExceeded` when the journal holds as many records as it may,
    /// counting the steps begun and not completed, which will each journal
    /// one. Checked before live work starts, so a call that could not be
    /// journaled never spends.
    pub(super) fn check_room(&self) -> Result<(), WorkflowError> {
        self.check_room_with(self.pending())
    }

    /// Steps begun and not completed or dropped.
    pub(super) fn pending(&self) -> u64 {
        self.inner.pending_steps.load(Ordering::SeqCst)
    }

    /// [`check_room`](Self::check_room) given the pending steps to count.
    pub(super) fn check_room_with(&self, pending: u64) -> Result<(), WorkflowError> {
        if let Some(max) = self.inner.max_records {
            let held = lock(&self.inner.journal).records.len() as u64;
            if held + pending >= max {
                return Err(WorkflowError::LimitExceeded {
                    what: "journal records",
                    max,
                });
            }
        }
        Ok(())
    }

    /// Make one result durable, then cache it.
    pub(super) async fn record(
        &self,
        call_key: &str,
        occurrence: u64,
        value: &JsonValue,
    ) -> Result<(), WorkflowError> {
        self.record_with(call_key, occurrence, value, self.pending())
            .await
    }

    /// [`record`](Self::record) given the pending steps to count: a step
    /// completing leaves its own place out.
    pub(super) async fn record_with(
        &self,
        call_key: &str,
        occurrence: u64,
        value: &JsonValue,
        pending: u64,
    ) -> Result<(), WorkflowError> {
        self.check_room_with(pending)?;
        let too_big = (self.inner.max_record_bytes).filter(|max| value.to_string().len() > *max);
        if let Some(max) = too_big {
            return Err(WorkflowError::LimitExceeded {
                what: "bytes in a journal record",
                max: max as u64,
            });
        }
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

tokio::task_local! {
    /// The fan-out branch path of the code being polled: the branch index
    /// at each level, outermost first, joined by `.`. Unset outside every
    /// fan-out, and in a task a branch spawns.
    static BRANCH: String;
}

/// The branch path of the code being polled; empty outside every fan-out.
fn branch_path() -> String {
    BRANCH.try_with(Clone::clone).unwrap_or_default()
}

/// Poll `future` as branch `index` of the fan-out being started in the
/// current branch.
pub(super) fn in_branch<F: Future>(index: usize, future: F) -> impl Future<Output = F::Output> {
    let parent = branch_path();
    let path = if parent.is_empty() {
        index.to_string()
    } else {
        format!("{parent}.{index}")
    };
    BRANCH.scope(path, future)
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
