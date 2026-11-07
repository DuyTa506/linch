//! The eval harness: run an [`Agent`](linch_core::Agent) over a list of
//! [`EvalCase`]s, score each run with [`Scorer`]s, and aggregate the
//! verdicts into an [`EvalResult`]; [`run_eval_benchmark`] runs one
//! [`EvalSuite`] against several agents.
//!
//! Every case runs on a fresh session, one case after another, so a
//! [`ScriptedProvider`](linch_core::ScriptedProvider) consumes its turns in
//! case order; within a case, a subagent's child run takes the turns
//! between its parent's `Subagent` call and the parent's next turn. A case
//! passes when its run records no error and no scorer
//! returns `Some(false)`; `None` is an unknown verdict and does not fail it.

mod benchmark;
mod harness;
mod scorers;
mod suite;
#[cfg(test)]
mod tests;

pub use benchmark::{
    EvalBenchmarkResult, EvalBenchmarkTarget, EvalTargetResult, run_eval_benchmark,
};
pub use harness::{CaseResult, EvalCase, EvalResult, Score, run_eval};
pub use scorers::Scorer;
pub use suite::{EvalSuite, SuiteError, load_eval_suite};
