//! The eval harness: run an [`Agent`](linch_core::Agent) over a list of
//! [`EvalCase`]s, score each run with [`Scorer`]s, and aggregate the
//! verdicts into an [`EvalResult`].
//!
//! Every case runs on a fresh session, one case after another, so a
//! [`ScriptedProvider`](linch_core::ScriptedProvider) consumes its turns in
//! case order. A case passes when its run records no error and no scorer
//! returns `Some(false)`; `None` is an unknown verdict and does not fail it.

mod harness;
mod scorers;
#[cfg(test)]
mod tests;

pub use harness::{CaseResult, EvalCase, EvalResult, Score, run_eval};
pub use scorers::Scorer;
