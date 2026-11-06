//! The built-in [`Scorer`]s.

use linch_core::Envelope;
use linch_core::events::kinds::ResultSubtype;
use serde::{Deserialize, Serialize};

use super::harness::{EvalCase, result_event};

/// A built-in check on one case's run.
///
/// Its serde form is the suite file's scorer entry: the variant name in
/// snake case under `type`, plus the variant's fields, for example
/// `{"type": "text_contains", "substring": "{expected}"}`. Unknown types and
/// unknown fields are rejected.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Scorer {
    /// Passes when the output contains `substring`, ignoring case. Each
    /// `{expected}` in it is replaced by the case's expected answer first.
    TextContains {
        /// Text to look for; `{expected}` when absent.
        #[serde(default = "expected_placeholder")]
        substring: String,
    },
    /// Passes when the run ended with a successful `result`; unknown when
    /// it ended without one (a suspended or broken run).
    RunCompleted {},
}

fn expected_placeholder() -> String {
    "{expected}".to_owned()
}

impl Scorer {
    /// The scorer's name in reports, as Python names it: the type, then
    /// `:` and its argument when it has one.
    pub fn name(&self) -> String {
        match self {
            Self::TextContains { substring } => format!("text_contains:{substring}"),
            Self::RunCompleted {} => "run_completed".to_owned(),
        }
    }

    /// Score one case's run.
    ///
    /// Args:
    ///     case: The case that ran, for its expected answer.
    ///     output: The run's final text.
    ///     events: Every envelope the run streamed.
    ///
    /// Returns:
    ///     `Some(true)` or `Some(false)`, or `None` when the run lacks the
    ///     data this scorer reads.
    pub fn score(&self, case: &EvalCase, output: &str, events: &[Envelope]) -> Option<bool> {
        match self {
            Self::TextContains { substring } => {
                let wanted = substring.replace("{expected}", &case.expected);
                Some(output.to_lowercase().contains(&wanted.to_lowercase()))
            }
            Self::RunCompleted {} => {
                result_event(events).map(|result| result.subtype == ResultSubtype::Success)
            }
        }
    }
}
