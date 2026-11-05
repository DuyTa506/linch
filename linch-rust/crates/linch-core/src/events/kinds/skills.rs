//! Skill lifecycle and workflow-journal kinds.

use super::is_zero;
use crate::json::JsonObject;
use serde::{Deserialize, Serialize};

/// The skills loaded for a request or session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillsLoadedEvent {
    /// Skill descriptors visible to the model.
    pub skills: Vec<JsonObject>,
}

/// A skill invocation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillInvokedEvent {
    /// Invoked skill name.
    pub name: String,
    /// Optional argument text supplied to the skill.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub args: Option<String>,
    /// Optional temporary model selected by the skill.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_override: Option<String>,
    /// Optional tool allowlist selected by the skill.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_tools: Option<Vec<String>>,
}

/// Completion of a skill invocation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillCompletedEvent {
    /// Completed skill name.
    pub name: String,
    /// Whether skill execution failed.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub is_error: bool,
}

/// Every [`WorkflowEvent::kind`].
///
/// The `*_end`/`*_replayed` kinds double as the resume journal: an
/// unchanged `wf.agent`/`wf.step` call prefix replays their cached
/// `result_text` on resume instead of re-executing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkflowKind {
    /// A named phase boundary.
    Phase,
    /// A `wf.agent` call started.
    AgentStart,
    /// A `wf.agent` call ended.
    AgentEnd,
    /// A `wf.agent` call was replayed from the resume journal.
    AgentReplayed,
    /// A `wf.step` call started.
    StepStart,
    /// A `wf.step` call ended.
    StepEnd,
    /// A `wf.step` call was replayed from the resume journal.
    StepReplayed,
    /// A `wf.interrupt` call requested external input.
    InterruptRequested,
    /// A pending interrupt was resolved.
    InterruptResolved,
    /// An interrupt resolution was replayed from the resume journal.
    InterruptReplayed,
}

/// Progress/journal event emitted by the workflow engine.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowEvent {
    /// Workflow lifecycle kind.
    pub kind: WorkflowKind,
    /// Human-readable event title.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub title: String,
    /// Stable workflow-call key used to correlate lifecycle records.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub call_key: String,
    /// Occurrence number distinguishing repeated calls with the same key.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub occurrence: u64,
    /// Subagent type associated with the event, if any.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub subagent_type: String,
    /// JSON-serialized step result used for replay, when present.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result_text: Option<String>,
    /// Structured step result, when present.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_output: Option<JsonObject>,
    /// Error from structured result handling, when present.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_error: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn optional_and_default_fields_are_omitted_when_empty() {
        let workflow = WorkflowEvent {
            kind: WorkflowKind::Phase,
            title: String::new(),
            call_key: String::new(),
            occurrence: 0,
            subagent_type: String::new(),
            result_text: None,
            structured_output: None,
            structured_error: None,
        };
        assert_eq!(
            serde_json::to_value(&workflow).unwrap(),
            json!({"kind": "phase"})
        );
    }
}
