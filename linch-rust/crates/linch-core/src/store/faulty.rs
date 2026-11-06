//! A store wrapper that fails one chosen write, for crash tests.
//!
//! [`Faulty`] wraps a [`RunStore`], a [`SessionStore`], or one value that is
//! both, and numbers writes from 1 across both traits: run `create`,
//! `claim` and `commit`, session `create` and `append`. Write `n` fails with
//! [`StoreError::Backend`] holding an [`InjectedFault`], either before it
//! reaches the inner store or after it has landed there. Reads and every
//! other write pass through.

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use async_trait::async_trait;
use thiserror::Error;

use crate::events::Envelope;
use crate::ids::{RunId, SessionId};
use crate::session_log::LogEntry;
use crate::store::{Commit, Lease, NewRun, RunRecord, RunStore, SessionStore, StoreError};

/// When the chosen write fails.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultTiming {
    /// The write never reaches the inner store.
    Before,
    /// The write lands in the inner store, then the caller sees an error.
    After,
}

/// The error a [`Faulty`] store reports for the chosen write.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
#[error("injected fault on write {write} ({timing:?})")]
pub struct InjectedFault {
    /// Number of the failed write, from 1.
    pub write: u64,
    /// Whether the write landed before the error.
    pub timing: FaultTiming,
}

/// A store that fails its `n`th write.
#[derive(Debug)]
pub struct Faulty<S: ?Sized> {
    inner: Arc<S>,
    fail_at: u64,
    timing: FaultTiming,
    writes: AtomicU64,
}

impl<S: ?Sized> Faulty<S> {
    /// Wrap `inner` so that write number `fail_at` (from 1) fails.
    ///
    /// Args:
    ///     inner: Store that receives every call that does not fail; keep a
    ///         clone of the `Arc` to inspect what landed.
    ///     fail_at: Number of the write to fail; 0 never fails.
    ///     timing: Whether that write lands before the error.
    pub fn new(inner: Arc<S>, fail_at: u64, timing: FaultTiming) -> Self {
        Self {
            inner,
            fail_at,
            timing,
            writes: AtomicU64::new(0),
        }
    }

    /// Writes attempted so far, the failed one included.
    pub fn writes(&self) -> u64 {
        self.writes.load(Ordering::SeqCst)
    }

    async fn write<T>(
        &self,
        op: impl Future<Output = Result<T, StoreError>>,
    ) -> Result<T, StoreError> {
        let write = self.writes.fetch_add(1, Ordering::SeqCst) + 1;
        if write != self.fail_at {
            return op.await;
        }
        if self.timing == FaultTiming::After {
            op.await?;
        }
        Err(StoreError::Backend(Box::new(InjectedFault {
            write,
            timing: self.timing,
        })))
    }
}

#[async_trait]
impl<S: RunStore + ?Sized> RunStore for Faulty<S> {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.write(self.inner.create(run)).await
    }

    async fn claim(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError> {
        self.write(self.inner.claim(namespace, run_id, owner)).await
    }

    async fn load(&self, namespace: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        self.inner.load(namespace, run_id).await
    }

    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.write(self.inner.commit(lease, commit)).await
    }

    async fn events(
        &self,
        namespace: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.inner.events(namespace, run_id, after_seq).await
    }
}

#[async_trait]
impl<S: SessionStore + ?Sized> SessionStore for Faulty<S> {
    async fn create(&self, namespace: &str, session_id: &SessionId) -> Result<(), StoreError> {
        self.write(self.inner.create(namespace, session_id)).await
    }

    async fn load(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<LogEntry>, StoreError> {
        self.inner.load(namespace, session_id).await
    }

    async fn append(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        let op = self
            .inner
            .append(namespace, session_id, expected_len, entries);
        self.write(op).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::message::Message;
    use crate::run::{Checkpoint, Phase};
    use crate::session_log::MessageEntry;
    use crate::store::memory::{MemoryRunStore, MemorySessionStore};
    use crate::usage::Usage;

    fn fault(error: StoreError) -> InjectedFault {
        match error {
            StoreError::Backend(source) => *source.downcast::<InjectedFault>().unwrap(),
            other => panic!("expected an injected fault, got {other:?}"),
        }
    }

    fn new_run() -> NewRun {
        NewRun {
            namespace: "ns".into(),
            run_id: RunId::new("r1"),
            session_id: SessionId::new("s"),
            parent_run_id: None,
            contract: "0".repeat(64).parse().unwrap(),
            checkpoint: Checkpoint {
                turn: 0,
                usage: Usage::default(),
                usage_complete: true,
                cost_usd: None,
                session_len: 0,
                phase: Phase::ProviderPending { attempt: 1 },
            },
            events: Vec::new(),
        }
    }

    #[tokio::test]
    async fn the_chosen_run_write_fails_before_it_lands() {
        let inner = Arc::new(MemoryRunStore::new());
        let store: Faulty<dyn RunStore> = Faulty::new(inner.clone(), 2, FaultTiming::Before);
        let id = RunId::new("r1");
        store.create(new_run()).await.unwrap();
        let error = store.claim("ns", &id, "w").await.unwrap_err();
        let expected = InjectedFault {
            write: 2,
            timing: FaultTiming::Before,
        };
        assert_eq!(fault(error), expected);
        assert_eq!(inner.load("ns", &id).await.unwrap().epoch, 0, "not landed");
        assert_eq!(store.claim("ns", &id, "w").await.unwrap().epoch, 1);
        store.load("ns", &id).await.unwrap();
        store.events("ns", &id, 0).await.unwrap();
        assert_eq!(store.writes(), 3, "reads are not counted");
    }

    #[tokio::test]
    async fn the_chosen_run_write_fails_after_it_lands() {
        let inner = Arc::new(MemoryRunStore::new());
        let store = Faulty::new(inner.clone(), 3, FaultTiming::After);
        let id = RunId::new("r1");
        store.create(new_run()).await.unwrap();
        let lease = store.claim("ns", &id, "w").await.unwrap();
        let commit = Commit {
            status: Some(crate::run::RunStatus::Completed),
            ..Commit::default()
        };
        let error = store.commit(&lease, commit).await.unwrap_err();
        assert_eq!(fault(error).write, 3);
        let record = inner.load("ns", &id).await.unwrap();
        assert_eq!(record.status, crate::run::RunStatus::Completed, "landed");
        assert_eq!(inner.writes().commit, 1);
    }

    #[tokio::test]
    async fn session_writes_share_the_count() {
        let inner = Arc::new(MemorySessionStore::new());
        let store = Faulty::new(inner.clone(), 2, FaultTiming::After);
        let id = SessionId::new("s");
        store.create("ns", &id).await.unwrap();
        let entry = LogEntry::Message(MessageEntry {
            message: Arc::new(Message::user_text("hi")),
            visible: true,
            historical: true,
        });
        let error = store.append("ns", &id, 0, vec![entry]).await.unwrap_err();
        assert_eq!(fault(error).timing, FaultTiming::After);
        assert_eq!(inner.load("ns", &id).await.unwrap().len(), 1, "landed");
        assert!(matches!(
            store.append("ns", &id, 0, Vec::new()).await,
            Err(StoreError::Conflict { .. })
        ));
    }
}
