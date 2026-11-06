//! SGLang, checked on 2026-11-06 against <https://docs.sglang.io> (the
//! OpenAI API, separate reasoning, tool parser and server arguments pages)
//! and the source at `main` 93cfea6b (release v0.5.23):
//! - **Reasoning** streams in `delta.reasoning_content` when the server runs
//!   with `--reasoning-parser`; assistant turns accept `reasoning_content`
//!   back, and the chat template reads it.
//! - **Thinking off:** `chat_template_kwargs` `enable_thinking: false`
//!   (Qwen3) or `thinking: false` (DeepSeek V3); both are sent, as SGLang
//!   itself does for `reasoning_effort: "none"`.
//! - **`reasoning_effort`** is accepted and passed to the template; any
//!   value but `none` turns thinking on.
//! - **`tool_choice`:** `auto`, `none`, `required` and a named function;
//!   tool calls come back as `tool_calls` only with `--tool-call-parser`.
//! - **Usage** comes on a last chunk with empty `choices` when
//!   `stream_options.include_usage` is set; `cached_tokens` needs
//!   `--enable-cache-report`.
//! - **Auth:** `Authorization: Bearer <key>` when started with `--api-key`.
//! - **Limit:** `max_completion_tokens` (`max_tokens` is deprecated).
//! - **Errors:** HTTP bodies are top-level `{"object": "error", "message",
//!   "type", "code"}`; mid-stream errors are `{"error": {...}}` with the
//!   HTTP status in `code`. A graceful abort ends with `finish_reason`
//!   `abort`.

use linch_core::JsonObject;
use serde_json::json;

use super::{kwargs, prompt_minus_cached};
use crate::openai::{ChatProfile, ToolChoiceSupport};
use crate::openai_compat::stream::ReasoningField;

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "sglang",
    reasoning_field: Some(ReasoningField::ReasoningContent),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: true,
    },
    max_tokens_field: "max_completion_tokens",
    reasoning_effort: true,
    template_thinking_switch: true,
    usage: prompt_minus_cached,
};

pub(super) fn thinking_off_kwargs() -> JsonObject {
    kwargs(json!({"enable_thinking": false, "thinking": false}))
}
