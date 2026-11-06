//! llama.cpp, checked on 2026-11-06 against `tools/server/README.md` and
//! the source at master 67107ac3
//! (<https://github.com/ggml-org/llama.cpp/tree/master/tools/server>):
//! - **Reasoning** streams in `delta.reasoning_content` (the default
//!   `--reasoning-format`); assistant turns accept `reasoning_content` back.
//! - **Thinking off:** `chat_template_kwargs` `enable_thinking: false`, the
//!   README's own example; the server reads that flag itself.
//! - **`reasoning_effort`** is accepted and passed to the template.
//! - **`tool_choice`:** `auto`, `none` and `required` only. A named
//!   function silently becomes `auto`, so it fails locally instead.
//! - **Usage** comes on a last chunk with empty `choices` only with
//!   `stream_options.include_usage`; `prompt_tokens` includes
//!   `prompt_tokens_details.cached_tokens` (reported since 2026-03-19).
//! - **Auth:** `Authorization: Bearer <key>` (or `X-Api-Key`) when started
//!   with `--api-key`.
//! - **Limit:** `max_tokens`, the documented field (`n_predict` and
//!   `max_completion_tokens` are aliases).
//! - **Tool calls** end with `tool_calls`; a call's name may be re-sent,
//!   longer, before its arguments start.
//! - **Errors:** HTTP bodies are `{"error": {"code", "message", "type"}}`; a
//!   mid-stream error is the same object in a `data:` frame, with no
//!   `[DONE]` after it.

use linch_core::JsonObject;
use serde_json::json;

use super::{kwargs, prompt_minus_cached};
use crate::openai::{ChatProfile, ToolChoiceSupport};
use crate::openai_compat::stream::ReasoningField;

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "llamacpp",
    reasoning_field: Some(ReasoningField::ReasoningContent),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: false,
    },
    max_tokens_field: "max_tokens",
    reasoning_effort: true,
    template_thinking_switch: true,
    usage: prompt_minus_cached,
};

pub(super) fn thinking_off_kwargs() -> JsonObject {
    kwargs(json!({"enable_thinking": false}))
}
