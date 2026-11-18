//! The decisions of the agent's reliability policy (see
//! [`crate::gates`]): a tool call refused for a missing prerequisite, and
//! a final answer sent back or stopped for an unmet completion gate.

use serde::{Deserialize, Serialize};

use super::is_zero;

/// Which rule of the policy decided.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyRule {
    /// A tool whose prerequisites have not succeeded yet.
    Prerequisite,
    /// A completion gate the final answer does not meet.
    Completion,
}

/// What the policy did.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyAction {
    /// The tool call was answered with an error and not run.
    Refuse,
    /// The final answer went back to the model with a correction, and the
    /// run goes on.
    Correct,
    /// The corrections are spent: the run ends failed.
    Stop,
}

/// Emitted when the reliability policy refuses a call, corrects a final
/// answer, or ends the run on a gate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PolicyEvent {
    /// Which rule decided.
    pub rule: PolicyRule,
    /// What it did.
    pub action: PolicyAction,
    /// The refused tool, for a prerequisite.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    /// The refused call, for a prerequisite.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_use_id: Option<String>,
    /// The gates the answer fails, for a completion rule.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gates: Vec<String>,
    /// The tools that have not succeeded yet: the ones the refused tool
    /// requires, or the ones the failing gates require.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub missing: Vec<String>,
    /// Corrections spent in this run once this event is counted.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub attempt: u64,
    /// What the model is told: the error result of a refused call, the
    /// correction, or why the run stopped.
    pub detail: String,
}
