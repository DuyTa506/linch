//! The durable workflow journal: the [`JournalStore`] trait, its records,
//! and the content keys that identify each journaled call.
//!
//! A workflow is keyed by `(namespace, workflow_id)`. Its row holds the
//! [`WorkflowStatus`] and a lease epoch; its records hold the results of
//! finished calls, keyed by `(call_key, occurrence)`. Only finished calls
//! are written, so the journal needs no fold on resume: the records *are*
//! the replay cache.
//!
//! Leases follow linch-core's model: [`JournalStore::claim`] always wins
//! and adds 1 to the epoch, and every write under an older epoch fails
//! with [`JournalStoreError::LeaseLost`]. There is no expiry and no
//! heartbeat.

use async_trait::async_trait;
use linch_core::{CanonicalError, JsonValue, digest_with_domain};
use serde::{Deserialize, Serialize};
use serde_json::json;
use thiserror::Error;

/// One finished call's result.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JournalRecord {
    /// Content key of the call ([`step_key`], [`interrupt_key`]).
    pub call_key: String,
    /// Which call with this key it was, counting from 0 in call order.
    pub occurrence: u64,
    /// The call's result as JSON.
    pub value: JsonValue,
}

/// The question a suspended workflow is waiting on.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Interrupt {
    /// The interrupt key the answer is supplied under.
    pub key: String,
    /// Context for whoever answers.
    pub payload: JsonValue,
}

/// Lifecycle status of a workflow.
///
/// The status is informational: any status can be claimed again, so a
/// failed run resumes from its journal and a completed one replays to the
/// end.
#[derive(Debug, Clone, PartialEq)]
pub enum WorkflowStatus {
    /// Claimed by a process and executing.
    Running,
    /// Parked at an unanswered interrupt; holds no process.
    AwaitingInput(Interrupt),
    /// The workflow function returned a value.
    Completed,
    /// The workflow function failed, timed out or was aborted.
    Failed,
}

impl WorkflowStatus {
    /// The status as its stored name: `running`, `awaiting_input`,
    /// `completed` or `failed`.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::AwaitingInput(_) => "awaiting_input",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }
}

/// A stored workflow.
#[derive(Debug, Clone, PartialEq)]
pub struct WorkflowRun {
    /// Current status.
    pub status: WorkflowStatus,
    /// Lease epoch; each claim adds 1, so the first claim gives 1.
    pub epoch: u64,
    /// Every journaled result, ordered by `(call_key, occurrence)`.
    pub records: Vec<JournalRecord>,
}

/// Proof of ownership for writes to one workflow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalLease {
    /// Key space.
    pub namespace: String,
    /// Leased workflow.
    pub workflow_id: String,
    /// Epoch at claim time. A later claim fences this lease out.
    pub epoch: u64,
}

/// A journal store operation failed.
#[derive(Debug, Error)]
pub enum JournalStoreError {
    /// A later claim fenced this lease out (or the workflow is gone).
    #[error("lease on workflow {workflow_id} lost (epoch {epoch})")]
    LeaseLost {
        /// Workflow whose lease was lost.
        workflow_id: String,
        /// Epoch of the stale lease.
        epoch: u64,
    },
    /// A record with the same `(call_key, occurrence)` already exists.
    #[error("conflict: {message}")]
    Conflict {
        /// What conflicted.
        message: String,
    },
    /// The storage backend failed.
    #[error("journal store backend error")]
    Backend(#[source] Box<dyn std::error::Error + Send + Sync>),
}

/// Durable workflow state: status, lease epoch and journal records.
///
/// Every write is fenced by the lease epoch. A rejected write changes
/// nothing.
#[async_trait]
pub trait JournalStore: Send + Sync {
    /// Take ownership of a workflow, creating it when it does not exist.
    ///
    /// Always succeeds against a live holder: the epoch goes up by 1, which
    /// fences out every earlier lease. The status becomes
    /// [`WorkflowStatus::Running`]; the records are kept.
    async fn claim(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<JournalLease, JournalStoreError>;

    /// Read the workflow, or `None` when it was never claimed.
    async fn load(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<Option<WorkflowRun>, JournalStoreError>;

    /// Add one record under `lease`.
    ///
    /// Raises:
    ///     JournalStoreError: `LeaseLost` under a stale epoch, `Conflict`
    ///         when the `(call_key, occurrence)` is already recorded.
    async fn append(
        &self,
        lease: &JournalLease,
        record: JournalRecord,
    ) -> Result<(), JournalStoreError>;

    /// Set the status under `lease`.
    ///
    /// Raises:
    ///     JournalStoreError: `LeaseLost` under a stale epoch.
    async fn set_status(
        &self,
        lease: &JournalLease,
        status: WorkflowStatus,
    ) -> Result<(), JournalStoreError>;
}

/// The content key of one `step` call: its name plus its optional input
/// key, so the same step over different inputs is journaled separately.
///
/// Domain-separated from [`interrupt_key`], so a step never collides with
/// an interrupt that shares its name. Objects are hashed in canonical (key
/// sorted) form; array order counts.
///
/// Raises:
///     CanonicalError: If `key` nests too deeply to encode.
pub fn step_key(name: &str, key: Option<&JsonValue>) -> Result<String, CanonicalError> {
    let value = json!({"name": name, "key": key});
    Ok(digest_with_domain("step", &value)?.to_string())
}

/// The content key of one `interrupt` call. Only the key is hashed: the
/// payload may be rebuilt differently across resumes without losing the
/// stored answer.
pub fn interrupt_key(key: &str) -> String {
    digest_with_domain("interrupt", &JsonValue::String(key.to_owned()))
        .expect("a JSON string always encodes")
        .to_string()
}
