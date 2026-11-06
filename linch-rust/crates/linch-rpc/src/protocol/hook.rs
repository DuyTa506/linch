//! Intercept callbacks: their declaration in `agent.open`, the `hook.call`
//! request, and each point's answer.

use std::num::NonZeroU64;

use linch::{
    ContentBlock, FailMode, HookPoint, JsonObject, JsonValue, TextBlock, ToolOutput,
    ToolOutputError, ToolResult,
};
use serde::{Deserialize, Serialize};

/// A callback the client serves at one hook point, declared in
/// `agent.open`.
///
/// The server calls it with `hook.call` only for occurrences its `matcher`
/// accepts; the others never cross the connection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackSpec {
    /// The point it intercepts.
    pub point: HookPoint,
    /// Stable id of the policy it enforces; with `policy_version` it enters
    /// the run contract.
    pub policy_id: String,
    /// Version of the policy. Bump it whenever the callback's behaviour,
    /// matcher, timeout or fail mode changes, so a resumed run notices.
    pub policy_version: u32,
    /// Tool-name globs (`*`, `?`); only at the tool points `pre_tool_use`,
    /// `permission_request` and `post_tool_use`. Absent: every tool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matcher: Option<Vec<String>>,
    /// Longest wait for an answer (default 30 000). A late answer is a
    /// failure, handled by the fail mode.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<NonZeroU64>,
    /// What an error or timeout means. Absent: the point's default, `closed`
    /// at `pre_tool_use` and `permission_request`, `open` elsewhere.
    /// `context_build` and `post_turn` cannot fail closed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fail_mode: Option<FailMode>,
}

/// Params of `hook.call`: one occurrence of a callback's point.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HookCallParams {
    /// Agent handle the callback was declared on.
    pub agent: String,
    /// The point that occurred.
    pub point: HookPoint,
    /// The callback's `policy_id`.
    pub policy_id: String,
    /// The occurrence; its shape depends on `point` (see the crate docs).
    pub request: JsonValue,
}

/// A tool result on the wire: `{"output": {"value", "metadata"?}}` or
/// `{"error": {"message", "code"?, "details"?}}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum WireToolResult {
    /// The tool succeeded.
    Output(ToolOutput),
    /// The tool failed; the message is what the model sees.
    Error(ToolOutputError),
}

/// A `pre_tool_use` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum PreToolUseAnswer {
    /// No change.
    Continue {},
    /// Use this input from here on; it is validated again.
    Rewrite {
        /// The new input.
        input: JsonObject,
    },
    /// Do not run the call; `reason` is shown to the model.
    Block {
        /// Why the call was blocked.
        reason: String,
    },
    /// Do not run the call; use this result. Permission still applies.
    Resolve {
        /// The precomputed result.
        result: WireToolResult,
    },
}

/// A `post_tool_use` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum PostToolUseAnswer {
    /// No change.
    Continue {},
    /// Use this result from here on.
    Replace {
        /// The new result.
        result: WireToolResult,
    },
}

/// A `user_prompt_submit` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum UserPromptAnswer {
    /// No change.
    Continue {},
    /// Use this prompt content from here on.
    Rewrite {
        /// The new content blocks.
        content: Vec<ContentBlock>,
    },
    /// Reject the prompt.
    Block {
        /// Why, for the host.
        reason: String,
    },
}

/// A `context_build` answer: text to add to this provider request only.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextBuildAnswer {
    /// Blocks to add; absent or empty adds nothing.
    #[serde(default)]
    pub blocks: Vec<TextBlock>,
}

/// A `stop` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum StopAnswer {
    /// Let the run stop.
    Allow {},
    /// Keep the run going; `feedback` is shown to the model.
    Continue {
        /// Why to keep going.
        feedback: String,
    },
}

/// A `post_turn` answer: `{}`. The point only observes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PostTurnAnswer {}

impl From<WireToolResult> for ToolResult {
    fn from(result: WireToolResult) -> Self {
        match result {
            WireToolResult::Output(output) => Ok(output),
            WireToolResult::Error(error) => Err(error),
        }
    }
}

impl From<&ToolResult> for WireToolResult {
    fn from(result: &ToolResult) -> Self {
        match result {
            Ok(output) => Self::Output(output.clone()),
            Err(error) => Self::Error(error.clone()),
        }
    }
}
