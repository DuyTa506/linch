//! [`MemoryJournalStore`]: the journal kept in process memory, for tests
//! and development.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;
use linch_core::JsonValue;

use super::journal::{
    JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowRun, WorkflowStatus,
};

/// A [`JournalStore`] kept in process memory. It passes the same
/// conformance suite as the Postgres store, so a workflow behaves the same
/// on both; nothing survives the process.
#[derive(Debug, Default)]
pub struct MemoryJournalStore {
    workflows: Mutex<HashMap<(String, String), Stored>>,
}

#[derive(Debug)]
struct Stored {
    status: WorkflowStatus,
    epoch: u64,
    records: BTreeMap<(String, u64), JsonValue>,
}

impl MemoryJournalStore {
    fn workflows(&self) -> MutexGuard<'_, HashMap<(String, String), Stored>> {
        // Every write is a single map update, so a panicking holder cannot
        // leave a half-applied change behind.
        self.workflows
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// The workflow `lease` holds, or `LeaseLost` when a later claim
    /// fenced it out.
    fn leased<'a>(
        workflows: &'a mut HashMap<(String, String), Stored>,
        lease: &JournalLease,
    ) -> Result<&'a mut Stored, JournalStoreError> {
        let key = (lease.namespace.clone(), lease.workflow_id.clone());
        match workflows.get_mut(&key) {
            Some(stored) if stored.epoch == lease.epoch => Ok(stored),
            _ => Err(JournalStoreError::LeaseLost {
                workflow_id: lease.workflow_id.clone(),
                epoch: lease.epoch,
            }),
        }
    }
}

#[async_trait]
impl JournalStore for MemoryJournalStore {
    async fn claim(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<JournalLease, JournalStoreError> {
        let mut workflows = self.workflows();
        let stored = workflows
            .entry((namespace.to_owned(), workflow_id.to_owned()))
            .or_insert_with(|| Stored {
                status: WorkflowStatus::Running,
                epoch: 0,
                records: BTreeMap::new(),
            });
        stored.epoch += 1;
        stored.status = WorkflowStatus::Running;
        Ok(JournalLease {
            namespace: namespace.to_owned(),
            workflow_id: workflow_id.to_owned(),
            epoch: stored.epoch,
        })
    }

    async fn load(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<Option<WorkflowRun>, JournalStoreError> {
        let workflows = self.workflows();
        let key = (namespace.to_owned(), workflow_id.to_owned());
        Ok(workflows.get(&key).map(|stored| WorkflowRun {
            status: stored.status.clone(),
            epoch: stored.epoch,
            records: stored
                .records
                .iter()
                .map(|((call_key, occurrence), value)| JournalRecord {
                    call_key: call_key.clone(),
                    occurrence: *occurrence,
                    value: value.clone(),
                })
                .collect(),
        }))
    }

    async fn append(
        &self,
        lease: &JournalLease,
        record: JournalRecord,
    ) -> Result<(), JournalStoreError> {
        let mut workflows = self.workflows();
        let stored = Self::leased(&mut workflows, lease)?;
        let key = (record.call_key, record.occurrence);
        if stored.records.contains_key(&key) {
            return Err(JournalStoreError::Conflict {
                message: format!(
                    "workflow {} already records {} #{}",
                    lease.workflow_id, key.0, key.1
                ),
            });
        }
        stored.records.insert(key, record.value);
        Ok(())
    }

    async fn set_status(
        &self,
        lease: &JournalLease,
        status: WorkflowStatus,
    ) -> Result<(), JournalStoreError> {
        let mut workflows = self.workflows();
        Self::leased(&mut workflows, lease)?.status = status;
        Ok(())
    }
}
