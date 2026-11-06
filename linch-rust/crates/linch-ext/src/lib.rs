//! Extensions built on the public API of the Linch kernel (`linch-core`).
//!
//! - [`evals`] runs an [`Agent`](linch_core::Agent) over eval cases, scores
//!   each run with built-in scorers, and compares one suite across agents.
//!
//! Every run here goes through [`Agent::run`](linch_core::Agent::run); for
//! offline, deterministic evals bind the agent to a
//! [`ScriptedProvider`](linch_core::ScriptedProvider) and memory stores.

#![forbid(unsafe_code)]

pub mod evals;

#[cfg(test)]
mod test_support;
