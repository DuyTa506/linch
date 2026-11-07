//! Directed workflows: host code owns the control flow, and every finished
//! call is journaled so the same workflow id replays it on resume.
//!
//! [`run_workflow`] runs an async function on a [`WorkflowContext`]. Its
//! journaled calls are [`WorkflowContext::step`] (host code with side
//! effects), [`WorkflowContext::agent`] (an agent's run on a prompt, which
//! a resume continues) and [`WorkflowContext::interrupt`] (a question for a
//! human);
//! [`WorkflowContext::parallel`] and [`WorkflowContext::settled`] fan
//! branches out, and [`WorkflowContext::phase`] marks progress. Everything
//! else in the function runs again on every resume.
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
//!     let diff = diff.await?;
//!     let approved: bool = wf.interrupt("approve", &diff).await?;
//!     Ok(if approved { format!("merged {diff}") } else { "closed".to_owned() })
//! }
//!
//! # async fn run() -> Result<(), WorkflowError> {
//! let journal = Arc::new(MemoryJournalStore::default());
//! let options = WorkflowOptions::new(journal.clone(), "tenant-a", "review-pr-42");
//! match run_workflow(options, review).await {
//!     Err(WorkflowError::Suspended(question)) => assert_eq!(question.key, "approve"),
//!     other => panic!("{other:?}"),
//! }
//! let mut options = WorkflowOptions::new(journal, "tenant-a", "review-pr-42");
//! options.resume.insert("approve".into(), true.into());
//! assert_eq!(run_workflow(options, review).await?, "merged diff");
//! # Ok(()) }
//! # let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build();
//! # runtime.unwrap().block_on(run()).unwrap();
//! ```
//!
//! ## Replay rule
//!
//! A call is identified by a content key ([`step_key`], [`agent_key`],
//! [`interrupt_key`])
//! plus its occurrence: how many calls with that key came before it in
//! this run. A resume replays every call whose key and occurrence the
//! journal holds, so the function must take the same path again: branching
//! on a journaled value is fine, branching on the clock, randomness or the
//! environment is not, unless that value comes from a step.
//!
//! Inside [`WorkflowContext::parallel`] and [`WorkflowContext::settled`],
//! each branch counts occurrences on its own, under its branch path
//! ([`JournalRecord::call_key`]), since branches reach their calls in
//! another order on replay. Concurrency built some other way (`join!` or
//! `select!`, or a spawned task, which does not inherit the path) gets no
//! path of its own, so calls there need distinct names or keys.
//!
//! ## Running across processes
//!
//! With a durable store, any process with the same workflow function can
//! continue a workflow: call [`run_workflow`] with the same namespace and
//! workflow id. Nothing re-invokes an interrupted workflow; the host finds
//! unfinished ones (status `running` or `failed`) and calls again. A claim
//! always wins and fences out the process that held the workflow before,
//! whose next journal write fails, so keep one runner per workflow (for
//! example with a job queue). A suspended workflow holds no process: any
//! process can read the question with [`JournalStore::load`] and answer it
//! through [`WorkflowOptions::resume`].
//!
//! ## Journal stores
//!
//! The journal lives in a [`JournalStore`]: [`MemoryJournalStore`] for
//! tests and development, or `PgJournalStore` behind the `postgres`
//! feature (run `linch_ext::migrate` once at startup). Every store passes
//! [`conformance::journal_store_suite`].

mod agent;
pub mod conformance;
mod context;
mod engine;
mod error;
mod fanout;
mod interrupt;
mod journal;
mod memory;
mod policy;
#[cfg(feature = "postgres")]
mod postgres;
mod step;
#[cfg(test)]
mod tests;

pub use context::{EventSink, WorkflowContext};
pub use engine::{WorkflowOptions, run_workflow};
pub use error::{BoxError, WorkflowError};
pub use journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowRun,
    WorkflowStatus, agent_key, interrupt_key, step_key,
};
pub use memory::MemoryJournalStore;
pub use policy::Retry;
#[cfg(feature = "postgres")]
pub use postgres::PgJournalStore;
pub use step::StepOptions;
