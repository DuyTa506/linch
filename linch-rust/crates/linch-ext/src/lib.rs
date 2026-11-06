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

#![forbid(unsafe_code)]

pub mod evals;
pub mod reports;
pub mod workflow;

#[cfg(test)]
mod test_support;
