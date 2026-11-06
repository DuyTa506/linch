//! [`run_eval_benchmark`]: one suite against several agents.

use std::time::Instant;

use linch_core::{Agent, JsonObject, JsonValue};
use serde_json::json;

use super::harness::{EvalResult, cell, percent, run_eval};
use super::suite::EvalSuite;

#[cfg(test)]
mod tests;

/// One agent to run a suite against.
#[derive(Clone)]
pub struct EvalBenchmarkTarget {
    /// Target name in the results.
    pub name: String,
    /// The agent under test.
    pub agent: Agent,
}

impl EvalBenchmarkTarget {
    /// A target called `name` running `agent`.
    pub fn new(name: impl Into<String>, agent: Agent) -> Self {
        Self {
            name: name.into(),
            agent,
        }
    }
}

/// The suite's results on one target.
#[derive(Debug, Clone, PartialEq)]
pub struct EvalTargetResult {
    /// The target's name.
    pub name: String,
    /// Every case's result.
    pub result: EvalResult,
    /// Wall time of the whole suite on this target, in milliseconds.
    pub duration_ms: f64,
}

impl EvalTargetResult {
    /// This result as JSON: name, duration (to the microsecond), totals,
    /// and the [`EvalResult::to_value`].
    pub fn to_value(&self, include_events: bool) -> JsonValue {
        json!({
            "name": self.name,
            "duration_ms": (self.duration_ms * 1000.0).round() / 1000.0,
            "total": self.result.total(),
            "passed": self.result.passed(),
            "pass_rate": self.result.pass_rate(),
            "result": self.result.to_value(include_events),
        })
    }
}

/// One suite's results across every target.
#[derive(Debug, Clone, PartialEq)]
pub struct EvalBenchmarkResult {
    /// The suite's name.
    pub suite_name: String,
    /// One result per target, in target order.
    pub targets: Vec<EvalTargetResult>,
    /// The suite's scorer names.
    pub scorer_names: Vec<String>,
    /// The suite's metadata.
    pub metadata: JsonObject,
}

impl EvalBenchmarkResult {
    /// Number of targets.
    pub fn total_targets(&self) -> usize {
        self.targets.len()
    }

    /// Number of targets that passed every case.
    pub fn passed_targets(&self) -> usize {
        self.targets
            .iter()
            .filter(|target| target.result.passed() == target.result.total())
            .count()
    }

    /// This result as JSON: the suite, its scorers and metadata, the target
    /// totals, and each [`EvalTargetResult::to_value`].
    pub fn to_value(&self, include_events: bool) -> JsonValue {
        let targets: Vec<JsonValue> = self
            .targets
            .iter()
            .map(|target| target.to_value(include_events))
            .collect();
        json!({
            "suite": self.suite_name,
            "scorers": self.scorer_names,
            "metadata": self.metadata,
            "total_targets": self.total_targets(),
            "passed_targets": self.passed_targets(),
            "targets": targets,
        })
    }

    /// A Markdown report: one summary row per target, then each target's
    /// [`EvalResult::to_markdown`] under its name.
    pub fn to_markdown(&self) -> String {
        let mut lines = vec![
            format!("# Linch Eval Benchmark: {}", self.suite_name),
            String::new(),
            format!("- targets: {}", self.total_targets()),
            format!("- passed_targets: {}", self.passed_targets()),
        ];
        if !self.scorer_names.is_empty() {
            lines.push(format!("- scorers: {}", self.scorer_names.join(", ")));
        }
        lines.extend([
            String::new(),
            "| Target | Cases | Passed | Pass Rate | Duration ms |".to_owned(),
            "|---|---:|---:|---:|---:|".to_owned(),
        ]);
        for target in &self.targets {
            lines.push(format!(
                "| {} | {} | {} | {} | {:.3} |",
                cell(&target.name),
                target.result.total(),
                target.result.passed(),
                percent(target.result.pass_rate()),
                target.duration_ms,
            ));
        }
        for target in &self.targets {
            lines.extend([
                String::new(),
                format!("## {}", target.name),
                String::new(),
                target.result.to_markdown(),
            ]);
        }
        lines.join("\n")
    }
}

/// Run `suite` against each target in turn, timing each.
///
/// Args:
///     suite: Cases and scorers to run.
///     targets: Agents to compare, in order. Each runs the cases on fresh
///         sessions through [`run_eval`].
///
/// Returns:
///     One [`EvalTargetResult`] per target, with the suite's name, scorer
///     names and metadata.
pub async fn run_eval_benchmark(
    suite: &EvalSuite,
    targets: &[EvalBenchmarkTarget],
) -> EvalBenchmarkResult {
    let mut results = Vec::with_capacity(targets.len());
    for target in targets {
        let started = Instant::now();
        let result = run_eval(&target.agent, &suite.cases, &suite.scorers).await;
        results.push(EvalTargetResult {
            name: target.name.clone(),
            result,
            duration_ms: started.elapsed().as_secs_f64() * 1000.0,
        });
    }
    EvalBenchmarkResult {
        suite_name: suite.name.clone(),
        targets: results,
        scorer_names: suite.scorer_names(),
        metadata: suite.metadata.clone(),
    }
}
