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
//!   Rechecked 2026-11-17 against
//!   <https://docs.sglang.io/advanced_features/tool_parser.html>, which
//!   documents `required` and a named function as forcing a call.
//! - **Usage** comes on a last chunk with empty `choices` when
//!   `stream_options.include_usage` is set; `cached_tokens` needs
//!   `--enable-cache-report`.
//! - **Auth:** `Authorization: Bearer <key>` when started with `--api-key`.
//! - **Limit:** `max_completion_tokens` (`max_tokens` is deprecated).
//! - **Errors:** HTTP bodies are top-level `{"object": "error", "message",
//!   "type", "code"}`; mid-stream errors are `{"error": {...}}` with the
//!   HTTP status in `code`. A graceful abort ends with `finish_reason`
//!   `abort`.
//! - **`output_schema`** is refused in a request: the profile sends no
//!   `response_format`, since the server's guided decoding would bind the
//!   tool-call text to the schema too, and that was not checked live.
//!   [`linch_core::Provider::native_output_schema`] is false, so a run with
//!   a schema offers its final tool instead.

use linch_core::JsonObject;
use serde_json::json;

use super::{kwargs, sglang_usage};
use crate::context::ContextRule;
use crate::models::OpenAiRow;
use crate::openai::{ChatProfile, ModelList, ToolChoiceSupport};
use crate::openai_compat::stream::ReasoningField;
use linch_core::{PromptCache, Support};

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "sglang",
    reasoning_field: Some(ReasoningField::ReasoningContent),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: true,
    },
    max_tokens_field: "max_completion_tokens",
    json_schema: false,
    reasoning_effort: true,
    template_thinking_switch: true,
    usage: sglang_usage,
    context: ContextRule::Models,
    overflow: &crate::overflow::SGLANG,
    vision: Support::ModelDependent,
    streams_tool_arguments: true,
    prompt_cache: PromptCache::Automatic,
    stop_sequences: true,
    models: ModelList {
        row: OpenAiRow {
            context: Some("max_model_len"),
            output: None,
            modalities: false,
        },
        props_context: false,
    },
};

pub(super) fn thinking_off_kwargs() -> JsonObject {
    kwargs(json!({"enable_thinking": false, "thinking": false}))
}
