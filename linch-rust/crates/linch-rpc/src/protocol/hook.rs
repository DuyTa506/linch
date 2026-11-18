//! Intercept callbacks: their declaration in `agent.open`, the `hook.call`
//! request, and each point's answer.

use std::num::NonZeroU64;

use linch::{
    AssistantAssembly, ContentBlock, FailMode, HookPoint, JsonObject, JsonValue, ProviderRequest,
    TextBlock, ToolOutput, ToolOutputError, ToolResult,
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
    /// `permission_request`, `post_tool_use` and `tool_execute`. Absent:
    /// every tool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matcher: Option<Vec<String>>,
    /// Longest wait for an answer. Absent: 30 seconds at `pre_tool_use` and
    /// `permission_request`, so a gate that never answers cannot hold the
    /// run; at every other point no limit, as in Python: the run waits until
    /// the client answers, the run is cancelled or the connection ends. A
    /// late answer is a failure, handled by the fail mode.
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
    /// Change part of the result and keep the rest (its metadata and
    /// error code): the content, the error flag, or both.
    Mutate {
        /// The new content: the output's `value`, or the error's
        /// `message` (a string as it is, any other value as compact JSON).
        /// Absent keeps it; `null` is refused as a bad answer, since it
        /// would read as absent.
        #[serde(
            default,
            skip_serializing_if = "Option::is_none",
            deserialize_with = "present_content"
        )]
        content: Option<JsonValue>,
        /// `true` turns an output into an error carrying its content as
        /// the message; `false` turns an error into an output carrying its
        /// message as a string value. Absent keeps the flag.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        is_error: Option<bool>,
    },
}

/// A `content` that is present must not be `null`: serde would read it as
/// absent, and the content would silently stay. Leave the member out to
/// keep the content.
fn present_content<'de, D>(deserializer: D) -> Result<Option<JsonValue>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    match JsonValue::deserialize(deserializer)? {
        JsonValue::Null => Err(serde::de::Error::custom(
            "mutate content must not be null; leave it out to keep the content",
        )),
        value => Ok(Some(value)),
    }
}

/// A `tool_execute` answer, by `action`. The point runs twice for a
/// callback that wants the result: a `before` call asked whether to run
/// what is inside, and an `after` call that carries the result.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum ToolExecuteAnswer {
    /// `before` only: run what is inside the callback (the layers after it
    /// and the tool). With `after`, the server then calls again with
    /// `phase: "after"` and the result.
    Next {
        /// Whether to see the result in a second call.
        #[serde(default)]
        after: bool,
    },
    /// `before` only: do not run what is inside; this is the result.
    /// Python's waterfall listener that does not call `next()`.
    Veto {
        /// The result the call gets.
        result: WireToolResult,
    },
    /// `after` only: keep the result.
    Continue {},
    /// `after` only: use this result instead.
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

/// A `before_provider_call` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum BeforeProviderCallAnswer {
    /// No change.
    Continue {},
    /// Send this request instead; later callbacks see it, and it is what
    /// the run's `context_build` event records.
    Rewrite {
        /// The new request.
        request: Box<ProviderRequest>,
    },
    /// Do not send the call, and take another turn instead: `feedback`
    /// goes to the model first. Python's `force_continue` here; bounded by
    /// `max_turns` and by the loop guard's `max_idle_turns`.
    ForceContinue {
        /// What the model is told before the next turn's call.
        feedback: String,
    },
    /// Do not send the call; end the run with this reason.
    Block {
        /// Why the call was blocked.
        reason: String,
    },
}

/// An `after_provider_call` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum AfterProviderCallAnswer {
    /// No change.
    Continue {},
    /// Use this response instead; it is what gets staged and appended to
    /// the session.
    Rewrite {
        /// The new response.
        assembly: Box<AssistantAssembly>,
    },
    /// Accept the response but take another turn instead of acting on it:
    /// none of its tool calls run, and `feedback` goes to the model first.
    Retry {
        /// What the model is told before it answers again.
        feedback: String,
    },
    /// The same as `retry`, under Python's other name for it here.
    ForceContinue {
        /// What the model is told before it answers again.
        feedback: String,
    },
    /// Do not accept the response; end the run with this reason.
    Block {
        /// Why the response was blocked.
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
    /// Details for observers, merged into the call's `context_build`
    /// event; never shown to the model.
    #[serde(default)]
    pub metadata: JsonObject,
}

/// A `stop` answer, by `action`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum StopAnswer {
    /// Let the run stop.
    Allow {},
    /// Keep the run going; `feedback` is shown to the model. Bounded only
    /// by `max_turns`.
    Continue {
        /// Why to keep going.
        feedback: String,
    },
    /// The same as `continue`, under Python's name for it at `stop`.
    ForceContinue {
        /// Why to keep going.
        feedback: String,
    },
    /// Ask for another answer, as Python's `before_final_answer` `retry`:
    /// honoured once per run call (with `spec.verification`, while its
    /// `max_retries` lasts), after which it counts as `allow`.
    Retry {
        /// What to fix.
        feedback: String,
    },
    /// Use this text as the run's final answer instead; later callbacks
    /// see it.
    Rewrite {
        /// The new final-answer text.
        text: String,
    },
    /// Do not let the run stop like this; end it with this reason.
    Block {
        /// Why the stop was blocked.
        reason: String,
    },
}

/// A `post_turn` answer: `{}`. The point only observes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PostTurnAnswer {}

/// An answer at any of the observe-only points added for parity with
/// Python's lifecycle hooks (`agent_start`, `agent_stop`, `turn_start`,
/// `provider_call_start`, `provider_call_stop`, `tool_use_start`,
/// `tool_use_stop`, `post_tool_use_failure`, `pre_compact`, `post_compact`,
/// `subagent_start`, `subagent_stop`, `event_emit`): `{}`. None of these can
/// fail closed, so the shape never carries a verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObserveAnswer {}

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

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn a_mutate_keeps_what_it_leaves_out_and_refuses_a_null_content() {
        let kept: PostToolUseAnswer = serde_json::from_value(json!({"action": "mutate"})).unwrap();
        let PostToolUseAnswer::Mutate { content, is_error } = kept else {
            panic!("a mutate");
        };
        assert_eq!((content, is_error), (None, None));
        let text = json!({"action": "mutate", "content": "x"});
        assert!(serde_json::from_value::<PostToolUseAnswer>(text).is_ok());
        let falsy = json!({"action": "mutate", "content": false});
        assert!(serde_json::from_value::<PostToolUseAnswer>(falsy).is_ok());
        let null = json!({"action": "mutate", "content": null});
        let refused = serde_json::from_value::<PostToolUseAnswer>(null).unwrap_err();
        assert!(
            refused.to_string().contains("must not be null"),
            "{refused}"
        );
    }
}
