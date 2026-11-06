//! Extensions built on the public API of the Linch kernel (`linch-core`).
//!
//! - [`evals`] runs an [`Agent`](linch_core::Agent) over eval cases, scores
//!   each run with built-in scorers, and compares one suite across agents.
//! - [`reports`] folds one run's event stream into a report of its tool
//!   calls, errors, usage and summary figures, as JSON or Markdown.
//! - [`workflow`] runs host code as a directed workflow whose finished
//!   steps and answered interrupts are journaled, so a resume replays them.
//!
//! Every run here goes through [`Agent::run`](linch_core::Agent::run); for
//! offline, deterministic evals bind the agent to a
//! [`ScriptedProvider`](linch_core::ScriptedProvider) and memory stores.
//!
//! The `postgres` feature adds the durable workflow journal
//! (`workflow::PgJournalStore`) and `migrate`, which creates its tables.

#![forbid(unsafe_code)]

pub mod evals;
#[cfg(feature = "postgres")]
mod postgres;
pub mod reports;
pub mod workflow;

#[cfg(feature = "postgres")]
pub use postgres::{MIGRATIONS_TABLE, migrate};
#[cfg(feature = "postgres")]
pub use sqlx::PgPool;
#[cfg(feature = "postgres")]
pub use sqlx::migrate::MigrateError;

#[cfg(test)]
mod test_support;
