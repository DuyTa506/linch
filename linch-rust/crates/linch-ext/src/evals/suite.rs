//! [`EvalSuite`]: cases and scorers as one JSON file.

use std::path::{Path, PathBuf};

use linch_core::JsonObject;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::harness::EvalCase;
use super::scorers::Scorer;

#[cfg(test)]
mod tests;

/// A named set of cases and the scorers applied to each.
///
/// The serde form is the suite file:
///
/// ```json
/// {
///   "name": "capitals",
///   "cases": [{"prompt": "Capital of France?", "expected": "Paris"}],
///   "scorers": [{"type": "text_contains"}, {"type": "run_completed"}]
/// }
/// ```
///
/// `cases` is required; a case's `expected` and `metadata`, and the
/// suite's `name`, `scorers` and `metadata`, may be left out. See
/// [`Scorer`] for the scorer entries. Unknown fields are rejected.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvalSuite {
    /// Suite name; [`load_eval_suite`] uses the file stem when it is empty.
    #[serde(default)]
    pub name: String,
    /// Cases, in run order.
    pub cases: Vec<EvalCase>,
    /// Scorers applied to every case.
    #[serde(default)]
    pub scorers: Vec<Scorer>,
    /// Free-form data about the suite, carried into benchmark results.
    #[serde(default)]
    pub metadata: JsonObject,
}

impl EvalSuite {
    /// Each scorer's [`Scorer::name`], in order.
    pub fn scorer_names(&self) -> Vec<String> {
        self.scorers.iter().map(Scorer::name).collect()
    }

    /// Decode a suite file's text.
    ///
    /// Raises:
    ///     SuiteError: If the text is not a suite, or a prompt, substring,
    ///         tool or metadata key is empty.
    pub fn from_json(text: &str) -> Result<Self, SuiteError> {
        let suite: Self = serde_json::from_str(text)?;
        suite.check()?;
        Ok(suite)
    }

    /// Reject the empty strings Python rejects: an empty substring or tool
    /// name would match everything or nothing.
    fn check(&self) -> Result<(), SuiteError> {
        for (index, case) in self.cases.iter().enumerate() {
            if case.prompt.is_empty() {
                return Err(SuiteError::Empty(format!("cases[{index}].prompt")));
            }
        }
        for (index, scorer) in self.scorers.iter().enumerate() {
            if let Some(field) = empty_field(scorer) {
                return Err(SuiteError::Empty(format!("scorers[{index}].{field}")));
            }
        }
        Ok(())
    }
}

/// The scorer's string field that is empty, if any.
fn empty_field(scorer: &Scorer) -> Option<&'static str> {
    match scorer {
        Scorer::TextContains { substring } if substring.is_empty() => Some("substring"),
        Scorer::ToolCalled { tool } | Scorer::ContextSelectedTool { tool } if tool.is_empty() => {
            Some("tool")
        }
        Scorer::RecoverySucceeded { tool: Some(tool) } if tool.is_empty() => Some("tool"),
        Scorer::ContextMetadataContains { key, .. } if key.is_empty() => Some("key"),
        _ => None,
    }
}

/// Load a suite from a JSON file, naming it after the file stem when the
/// file gives no name.
///
/// Raises:
///     SuiteError: If the file cannot be read or is not a valid suite.
pub fn load_eval_suite(path: impl AsRef<Path>) -> Result<EvalSuite, SuiteError> {
    let path = path.as_ref();
    let text = std::fs::read_to_string(path).map_err(|source| SuiteError::Read {
        path: path.to_owned(),
        source,
    })?;
    let mut suite = EvalSuite::from_json(&text)?;
    if suite.name.is_empty() {
        let stem = path.file_stem().unwrap_or_default();
        suite.name = stem.to_string_lossy().into_owned();
    }
    Ok(suite)
}

/// A suite file could not be loaded.
#[derive(Debug, Error)]
pub enum SuiteError {
    /// The file could not be read.
    #[error("suite file {} could not be read: {source}", path.display())]
    Read {
        /// The file.
        path: PathBuf,
        /// Why reading failed.
        #[source]
        source: std::io::Error,
    },
    /// The text is not JSON in the suite schema.
    #[error("invalid suite: {0}")]
    Decode(#[from] serde_json::Error),
    /// A string field that must not be empty is empty.
    #[error("invalid suite: {0} must be a non-empty string")]
    Empty(String),
}
