//! Directed workflows: host code owns the control flow, and every finished
//! call is journaled so the same workflow id replays it on resume.
//!
//! The journal lives in a [`JournalStore`]: [`MemoryJournalStore`] for
//! tests and development, or `PgJournalStore` behind the `postgres`
//! feature.

pub mod conformance;
mod journal;
mod memory;
#[cfg(test)]
mod tests;

pub use journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowRun,
    WorkflowStatus, interrupt_key, step_key,
};
pub use memory::MemoryJournalStore;
