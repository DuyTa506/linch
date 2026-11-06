//! In-memory run and session stores, for tests and development.
//!
//! Both follow the lifecycle that [`super::conformance`] checks and count
//! every write that lands, per method, so a bench can report store writes
//! per turn. A rejected write changes nothing and is not counted.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;

use crate::events::Envelope;
use crate::ids::{RunId, SessionId};
use crate::run::RunStatus;
use crate::session_log::LogEntry;
use crate::store::{Commit, Lease, NewRun, RunRecord, RunStore, SessionStore, StoreError};

#[cfg(test)]
mod tests;

/// Landed writes per [`RunStore`] method.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RunWrites {
    /// Successful `create` calls.
    pub create: u64,
    /// Successful `claim` calls.
    pub claim: u64,
    /// Successful `commit` calls.
    pub commit: u64,
}

/// Landed writes per [`SessionStore`] method.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SessionWrites {
    /// Successful `create` calls.
    pub create: u64,
    /// Successful `append` calls.
    pub append: u64,
}

/// A [`RunStore`] kept in process memory.
#[derive(Debug, Default)]
pub struct MemoryRunStore {
    state: Mutex<RunState>,
}

#[derive(Debug, Default)]
struct RunState {
    runs: HashMap<String, HashMap<RunId, StoredRun>>,
    writes: RunWrites,
}

#[derive(Debug)]
struct StoredRun {
    record: RunRecord,
    events: Vec<Envelope>,
}

impl MemoryRunStore {
    /// An empty store.
    pub fn new() -> Self {
        Self::default()
    }

    /// Writes that have landed so far.
    pub fn writes(&self) -> RunWrites {
        lock(&self.state).writes
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code path panics while holding the lock, so the data stays whole.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn not_found(kind: &'static str, namespace: &str, id: &str) -> StoreError {
    StoreError::NotFound {
        kind,
        namespace: namespace.to_owned(),
        id: id.to_owned(),
    }
}

fn find<'a>(
    runs: &'a mut HashMap<String, HashMap<RunId, StoredRun>>,
    namespace: &str,
    run_id: &RunId,
) -> Result<&'a mut StoredRun, StoreError> {
    runs.get_mut(namespace)
        .and_then(|space| space.get_mut(run_id))
        .ok_or_else(|| not_found("run", namespace, run_id.as_str()))
}

/// Check that `events` belong to `run_id` and continue `last_seq`.
fn check_events(run_id: &RunId, last_seq: u64, events: &[Envelope]) -> Result<(), StoreError> {
    for (expected, event) in (last_seq + 1..).zip(events) {
        if event.run_id != *run_id {
            return Err(StoreError::Conflict {
                message: format!("event for run {} written to run {run_id}", event.run_id),
            });
        }
        if event.seq != expected {
            return Err(StoreError::SeqGap {
                expected,
                got: event.seq,
            });
        }
    }
    Ok(())
}

#[async_trait]
impl RunStore for MemoryRunStore {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        check_events(&run.run_id, 0, &run.events)?;
        let mut state = lock(&self.state);
        let space = state.runs.entry(run.namespace.clone()).or_default();
        if space.contains_key(&run.run_id) {
            return Err(StoreError::Conflict {
                message: format!("run {} already exists in {}", run.run_id, run.namespace),
            });
        }
        let active = (space.values().map(|stored| &stored.record))
            .find(|record| record.session_id == run.session_id && !record.status.is_terminal());
        if let Some(active) = active {
            return Err(StoreError::SessionBusy {
                session_id: run.session_id,
                run_id: active.run_id.clone(),
            });
        }
        let record = RunRecord {
            namespace: run.namespace,
            run_id: run.run_id,
            session_id: run.session_id,
            parent_run_id: run.parent_run_id,
            contract: run.contract,
            status: RunStatus::Pending,
            epoch: 0,
            checkpoint: run.checkpoint,
            last_seq: run.events.len() as u64,
        };
        let stored = StoredRun {
            record: record.clone(),
            events: run.events,
        };
        space.insert(record.run_id.clone(), stored);
        state.writes.create += 1;
        Ok(record)
    }

    async fn claim(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError> {
        let mut state = lock(&self.state);
        let RunState { runs, writes } = &mut *state;
        let record = &mut find(runs, namespace, run_id)?.record;
        if record.status.is_terminal() {
            return Err(StoreError::InvalidTransition {
                from: record.status,
                to: RunStatus::Running,
            });
        }
        record.status = RunStatus::Running;
        record.epoch += 1;
        writes.claim += 1;
        Ok(Lease {
            namespace: namespace.to_owned(),
            run_id: run_id.clone(),
            owner: owner.to_owned(),
            epoch: record.epoch,
        })
    }

    async fn load(&self, namespace: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        let mut state = lock(&self.state);
        Ok(find(&mut state.runs, namespace, run_id)?.record.clone())
    }

    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        let mut state = lock(&self.state);
        let RunState { runs, writes } = &mut *state;
        let run = find(runs, &lease.namespace, &lease.run_id)?;
        let record = &mut run.record;
        if lease.epoch != record.epoch {
            return Err(StoreError::LeaseLost {
                run_id: lease.run_id.clone(),
                epoch: lease.epoch,
            });
        }
        let from = record.status;
        let to = commit.status.unwrap_or(from);
        if from != RunStatus::Running || to == RunStatus::Pending {
            return Err(StoreError::InvalidTransition { from, to });
        }
        check_events(&lease.run_id, record.last_seq, &commit.events)?;
        record.last_seq += commit.events.len() as u64;
        record.status = to;
        if let Some(checkpoint) = commit.checkpoint {
            record.checkpoint = checkpoint;
        }
        run.events.extend(commit.events);
        writes.commit += 1;
        Ok(())
    }

    async fn events(
        &self,
        namespace: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        let mut state = lock(&self.state);
        let events = &find(&mut state.runs, namespace, run_id)?.events;
        // Stored seqs run 1..=len, so seq > after_seq starts at index after_seq.
        let start = usize::try_from(after_seq).map_or(events.len(), |n| n.min(events.len()));
        Ok(events[start..].to_vec())
    }
}

/// A [`SessionStore`] kept in process memory.
#[derive(Debug, Default)]
pub struct MemorySessionStore {
    state: Mutex<SessionState>,
}

#[derive(Debug, Default)]
struct SessionState {
    logs: HashMap<String, HashMap<SessionId, Vec<LogEntry>>>,
    writes: SessionWrites,
}

impl MemorySessionStore {
    /// An empty store.
    pub fn new() -> Self {
        Self::default()
    }

    /// Writes that have landed so far.
    pub fn writes(&self) -> SessionWrites {
        lock(&self.state).writes
    }
}

fn find_log<'a>(
    logs: &'a mut HashMap<String, HashMap<SessionId, Vec<LogEntry>>>,
    namespace: &str,
    session_id: &SessionId,
) -> Result<&'a mut Vec<LogEntry>, StoreError> {
    logs.get_mut(namespace)
        .and_then(|space| space.get_mut(session_id))
        .ok_or_else(|| not_found("session", namespace, session_id.as_str()))
}

#[async_trait]
impl SessionStore for MemorySessionStore {
    async fn create(&self, namespace: &str, session_id: &SessionId) -> Result<(), StoreError> {
        let mut state = lock(&self.state);
        let space = state.logs.entry(namespace.to_owned()).or_default();
        if space.contains_key(session_id) {
            return Err(StoreError::Conflict {
                message: format!("session {session_id} already exists in {namespace}"),
            });
        }
        space.insert(session_id.clone(), Vec::new());
        state.writes.create += 1;
        Ok(())
    }

    async fn load(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<LogEntry>, StoreError> {
        let mut state = lock(&self.state);
        Ok(find_log(&mut state.logs, namespace, session_id)?.clone())
    }

    async fn append(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        let mut state = lock(&self.state);
        let SessionState { logs, writes } = &mut *state;
        let log = find_log(logs, namespace, session_id)?;
        let len = log.len() as u64;
        if len != expected_len {
            return Err(StoreError::Conflict {
                message: format!("session {session_id} has {len} entries, expected {expected_len}"),
            });
        }
        log.extend(entries);
        writes.append += 1;
        Ok(log.len() as u64)
    }
}
