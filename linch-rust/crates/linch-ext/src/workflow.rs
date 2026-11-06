//! Directed workflows: host code owns the control flow, and every finished
//! call is journaled so the same workflow id replays it on resume.
//!
//! [`run_workflow`] runs an async function on a [`WorkflowContext`]. Its
//! journaled calls are [`WorkflowContext::step`] (host code with side
//! effects); everything else in the function runs again on every resume.
//!
//! ```
//! use std::sync::Arc;
//!
//! use linch_ext::workflow::{
//!     MemoryJournalStore, WorkflowContext, WorkflowError, WorkflowOptions, run_workflow,
//! };
//!
//! async fn review(wf: WorkflowContext) -> Result<String, WorkflowError> {
//!     wf.phase("Find");
//!     let diff = wf.step("fetch", || async { Ok::<_, std::io::Error>("diff".to_owned()) });
//!     Ok(format!("reviewed {}", diff.await?))
//! }
//!
//! # async fn run() -> Result<(), WorkflowError> {
//! let journal = Arc::new(MemoryJournalStore::default());
//! let options = WorkflowOptions::new(journal, "tenant-a", "review-pr-42");
//! assert_eq!(run_workflow(options, review).await?, "reviewed diff");
//! # Ok(()) }
//! ```
//!
//! ## Replay rule
//!
//! A call is identified by a content key ([`step_key`]) plus its
//! occurrence: how many calls with that key came before it in this run.
//! A resume replays every call whose key and occurrence the journal holds,
//! so the function must take the same path again: branching on a
//! journaled value is fine, branching on the clock, randomness or the
//! environment is not, unless that value comes from a step.
//!
//! ## Journal stores
//!
//! The journal lives in a [`JournalStore`]: [`MemoryJournalStore`] for
//! tests and development, or `PgJournalStore` behind the `postgres`
//! feature. Every store passes [`conformance::journal_store_suite`].

pub mod conformance;
mod context;
mod engine;
mod error;
mod journal;
mod memory;
mod policy;
#[cfg(test)]
mod tests;

pub use context::{EventSink, StepOptions, WorkflowContext};
pub use engine::{WorkflowOptions, run_workflow};
pub use error::{BoxError, WorkflowError};
pub use journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowRun,
    WorkflowStatus, interrupt_key, step_key,
};
pub use memory::MemoryJournalStore;
pub use policy::Retry;
