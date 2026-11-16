//! Monitoring methods: `eval.run` and `run.report`.

use linch::ext::evals::{EvalCase, Scorer};
use linch::{JsonValue, RunId};
use serde::{Deserialize, Serialize};

/// Run a list of cases on an open agent and score them.
pub const EVAL_RUN: &str = "eval.run";
/// Build the report of one stored run.
pub const RUN_REPORT: &str = "run.report";

/// Params of `eval.run`.
///
/// Each case runs on a fresh session of the agent, one after another, as
/// `linch::ext::evals::run_eval` does; the runs are stored like any run, so
/// each case's `run_id` can be read with `run.report`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvalRunParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// Cases to run, in order: `{prompt, expected?, metadata?}`. A prompt
    /// must not be empty.
    pub cases: Vec<EvalCase>,
    /// Built-in scorers applied to every case, such as
    /// `{"type": "text_contains", "substring": "{expected}"}`; with none a
    /// case passes when its run records no error. An unknown type or field,
    /// or an empty substring, tool or key, is invalid params.
    #[serde(default)]
    pub scorers: Vec<Scorer>,
    /// Whether each case carries every event of its run; off when absent.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub include_events: bool,
}

/// Result of `eval.run`: the eval result as JSON (`total`, `passed`,
/// `pass_rate`, `total_cost_usd`, `usage`, `duration_ms`, `cases`) plus its
/// Markdown report under `markdown`.
pub type EvalReport = JsonValue;

/// Params of `run.report`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// The stored run to report on.
    pub run_id: RunId,
    /// Whether the report carries every event under `timeline`; off when
    /// absent.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub timeline: bool,
}

/// Result of `run.report`: `{report, markdown}`, the run report as JSON
/// and as Markdown.
pub type RunReportResult = JsonValue;
