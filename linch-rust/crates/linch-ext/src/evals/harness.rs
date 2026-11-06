//! [`run_eval`] and its result types.

use futures::StreamExt;
use linch_core::events::kinds::ResultEvent;
use linch_core::{Agent, Envelope, Event, JsonObject, JsonValue, RunOptions, SessionId};
use serde::{Deserialize, Serialize};
use serde_json::json;
use uuid::Uuid;

use super::scorers::Scorer;

/// One eval case: a prompt for a fresh session and the answer scorers may
/// compare against.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvalCase {
    /// Prompt the agent runs on.
    pub prompt: String,
    /// Expected answer; [`Scorer::TextContains`] puts it in place of
    /// `{expected}`.
    #[serde(default)]
    pub expected: String,
    /// Free-form data about the case, carried into the results.
    #[serde(default)]
    pub metadata: JsonObject,
}

impl EvalCase {
    /// A case for `prompt` expecting `expected`, with no metadata.
    pub fn new(prompt: impl Into<String>, expected: impl Into<String>) -> Self {
        Self {
            prompt: prompt.into(),
            expected: expected.into(),
            metadata: JsonObject::new(),
        }
    }
}

/// One scorer's verdict on one case.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Score {
    /// The scorer's [`Scorer::name`].
    pub name: String,
    /// `Some(true)` passes, `Some(false)` fails, `None` is unknown: the run
    /// lacks the data the scorer reads.
    pub verdict: Option<bool>,
}

/// The outcome of one case.
#[derive(Debug, Clone, PartialEq)]
pub struct CaseResult {
    /// The case that ran.
    pub case: EvalCase,
    /// The run's final text; empty when it produced none.
    pub output: String,
    /// Whether the run recorded no error and no scorer returned
    /// `Some(false)`.
    pub passed: bool,
    /// One verdict per scorer, in scorer order.
    pub scores: Vec<Score>,
    /// Every envelope the run streamed, in order.
    pub events: Vec<Envelope>,
    /// The last error the run reported: an `error` event's message, or the
    /// [`RunError`](linch_core::RunError) that ended the stream.
    pub error: Option<String>,
}

impl CaseResult {
    /// Names of the tools the run started, in call order.
    pub fn tool_calls(&self) -> Vec<&str> {
        self.events
            .iter()
            .filter_map(|envelope| match &envelope.event {
                Event::ToolCallStart(start) => Some(start.tool_name.as_str()),
                _ => None,
            })
            .collect()
    }

    /// This result as JSON: the case, output, verdict, scores, error, the
    /// event count and the tools called, plus every event when
    /// `include_events` is set. An event that cannot be encoded (a
    /// non-finite cost) is written as `null`.
    pub fn to_value(&self, include_events: bool) -> JsonValue {
        let mut value = json!({
            "case": self.case,
            "output": self.output,
            "passed": self.passed,
            "scores": self.scores,
            "error": self.error,
            "event_count": self.events.len(),
            "tool_calls": self.tool_calls(),
        });
        if include_events {
            let events = self
                .events
                .iter()
                .map(|envelope| envelope.to_value().unwrap_or(JsonValue::Null));
            value["events"] = JsonValue::Array(events.collect());
        }
        value
    }
}

/// The outcome of every case of one [`run_eval`].
#[derive(Debug, Clone, PartialEq)]
pub struct EvalResult {
    /// One result per case, in case order.
    pub cases: Vec<CaseResult>,
}

impl EvalResult {
    /// Number of cases.
    pub fn total(&self) -> usize {
        self.cases.len()
    }

    /// Number of cases that passed.
    pub fn passed(&self) -> usize {
        self.cases.iter().filter(|case| case.passed).count()
    }

    /// Passed over total; 0 when there are no cases.
    pub fn pass_rate(&self) -> f64 {
        if self.cases.is_empty() {
            return 0.0;
        }
        self.passed() as f64 / self.total() as f64
    }

    /// This result as JSON: the totals and each case's
    /// [`CaseResult::to_value`].
    pub fn to_value(&self, include_events: bool) -> JsonValue {
        let cases: Vec<JsonValue> = self
            .cases
            .iter()
            .map(|case| case.to_value(include_events))
            .collect();
        json!({
            "total": self.total(),
            "passed": self.passed(),
            "pass_rate": self.pass_rate(),
            "cases": cases,
        })
    }

    /// A Markdown report: the totals, then one table row per case.
    pub fn to_markdown(&self) -> String {
        let mut lines = vec![
            "# Linch Eval Report".to_owned(),
            String::new(),
            format!("- total: {}", self.total()),
            format!("- passed: {}", self.passed()),
            format!("- pass_rate: {}", percent(self.pass_rate())),
            String::new(),
            "| # | Passed | Scores | Tool Calls | Output | Error |".to_owned(),
            "|---:|---|---|---|---|---|".to_owned(),
        ];
        for (index, case) in self.cases.iter().enumerate() {
            let scores: Vec<String> = case
                .scores
                .iter()
                .map(|score| format!("{}={}", score.name, verdict(score.verdict)))
                .collect();
            lines.push(format!(
                "| {} | {} | {} | {} | {} | {} |",
                index + 1,
                case.passed,
                cell(&scores.join(", ")),
                cell(&case.tool_calls().join(", ")),
                cell(&case.output),
                cell(case.error.as_deref().unwrap_or("")),
            ));
        }
        lines.join("\n")
    }
}

/// Run `agent` over `cases` one after another and score each run.
///
/// Each case runs on a new session with a fresh id, which
/// [`Agent::close_session`] releases once the run ends; the agent releases
/// the session of every child run its `Subagent` calls ran. A child runs on the
/// agent's provider, so a scripted provider gives it the turn after the
/// parent's `Subagent` call, and the parent the turn after the child's
/// last. A run that suspends for approval is left suspended.
///
/// Args:
///     agent: The agent under test.
///     cases: Cases to run, in order.
///     scorers: Scorers applied to every case; with none, a case passes
///         when its run records no error.
///
/// Returns:
///     One [`CaseResult`] per case, in case order.
pub async fn run_eval(agent: &Agent, cases: &[EvalCase], scorers: &[Scorer]) -> EvalResult {
    let mut results = Vec::with_capacity(cases.len());
    for case in cases {
        results.push(run_case(agent, case, scorers).await);
    }
    EvalResult { cases: results }
}

async fn run_case(agent: &Agent, case: &EvalCase, scorers: &[Scorer]) -> CaseResult {
    let session = SessionId::new(format!("eval-{}", Uuid::new_v4()));
    let mut stream = agent.run(case.prompt.clone(), RunOptions::new(session.clone()));
    let mut events = Vec::new();
    let mut error = None;
    while let Some(item) = stream.next().await {
        match item {
            Ok(envelope) => {
                if let Event::Error(event) = &envelope.event {
                    error = Some(error_text(&event.error));
                }
                events.push(envelope);
            }
            Err(run_error) => {
                error = Some(run_error.to_string());
                break;
            }
        }
    }
    agent.close_session(&session);
    let output = result_event(&events)
        .and_then(|result| result.final_text.clone())
        .unwrap_or_default();
    let scores: Vec<Score> = scorers
        .iter()
        .map(|scorer| Score {
            name: scorer.name(),
            verdict: scorer.score(case, &output, &events),
        })
        .collect();
    let passed = error.is_none() && scores.iter().all(|score| score.verdict != Some(false));
    CaseResult {
        case: case.clone(),
        output,
        passed,
        scores,
        events,
        error,
    }
}

/// The run's `result` event, if it reached one.
pub(super) fn result_event(events: &[Envelope]) -> Option<&ResultEvent> {
    events
        .iter()
        .rev()
        .find_map(|envelope| match &envelope.event {
            Event::Result(result) => Some(result),
            _ => None,
        })
}

/// An error payload's `message`, or the payload as JSON when it has none.
fn error_text(error: &JsonObject) -> String {
    match error.get("message") {
        Some(JsonValue::String(message)) => message.clone(),
        _ => JsonValue::Object(error.clone()).to_string(),
    }
}

fn verdict(verdict: Option<bool>) -> &'static str {
    match verdict {
        Some(true) => "true",
        Some(false) => "false",
        None => "none",
    }
}

/// `rate` as a percentage with two decimals, like Python's `{:.2%}`.
pub(super) fn percent(rate: f64) -> String {
    format!("{:.2}%", rate * 100.0)
}

/// One Markdown table cell: whitespace collapsed, at most 120 characters,
/// pipes escaped.
pub(super) fn cell(value: &str) -> String {
    let text = value.split_whitespace().collect::<Vec<_>>().join(" ");
    let text = if text.chars().count() > 120 {
        let head: String = text.chars().take(117).collect();
        format!("{head}...")
    } else {
        text
    };
    text.replace('|', "\\|")
}
