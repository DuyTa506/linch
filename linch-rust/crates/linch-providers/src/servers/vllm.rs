//! vLLM, checked on 2026-11-06 against <https://docs.vllm.ai/en/latest>
//! (reasoning outputs, tool calling, OpenAI-compatible server, security
//! and `vllm serve` pages) and the source at main 8d5b5f1 (v0.33.0):
//! - **Reasoning** streams in `delta.reasoning` only: `reasoning_content`
//!   was renamed in v0.11.1 and left the output in v0.16.0. Assistant
//!   turns still accept `reasoning_content` back (renamed on input since
//!   v0.22.0).
//! - **Thinking off:** `chat_template_kwargs` `enable_thinking: false`, as
//!   the reasoning docs give for Qwen3.
//! - **`reasoning_effort`** takes `none` to `max` and is passed to the
//!   template; any value but `none` sets `enable_thinking`.
//! - **`tool_choice`:** `auto`, `none`, `required` and a named function.
//!   In practice all but `none` need `--enable-auto-tool-choice` and
//!   `--tool-call-parser`; without them the server answers 400.
//! - **Usage** comes on a last chunk with empty `choices` only with
//!   `stream_options.include_usage`. `prompt_tokens_details`
//!   (`cached_tokens`, `created_cache_tokens`) needs
//!   `--enable-prompt-tokens-details`.
//! - **Auth:** `Authorization: Bearer <key>` only, on `/v1` paths, when
//!   started with `--api-key` or `VLLM_API_KEY`.
//! - **Limit:** `max_completion_tokens` (`max_tokens` is deprecated).
//! - **Errors:** `{"error": {"message", "type", "param", "code"}}` with
//!   the HTTP status as an integer `code`, also as a mid-stream frame
//!   before `[DONE]`. `finish_reason` may be `abort` or `repetition`.

use linch_core::{JsonObject, Usage};
use serde_json::json;

use super::kwargs;
use crate::openai::{ChatProfile, ToolChoiceSupport};
use crate::openai_compat::stream::{ReasoningField, WireUsage};

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "vllm",
    reasoning_field: Some(ReasoningField::Reasoning),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: true,
    },
    max_tokens_field: "max_completion_tokens",
    reasoning_effort: true,
    template_thinking_switch: true,
    usage,
};

pub(super) fn thinking_off_kwargs() -> JsonObject {
    kwargs(json!({"enable_thinking": false}))
}

/// `prompt_tokens` includes cache reads (`cached_tokens`) and the tokens
/// written to the prefix cache (`created_cache_tokens`, counted as a cache
/// write as the Python adapter did; the docs do not define it further).
fn usage(wire: &WireUsage) -> Usage {
    let details = wire.prompt_tokens_details.as_ref();
    let read = details
        .and_then(|details| details.cached_tokens)
        .unwrap_or(0);
    let created = details
        .and_then(|details| details.created_cache_tokens)
        .unwrap_or(0);
    Usage {
        input_tokens: wire
            .prompt_tokens
            .saturating_sub(read)
            .saturating_sub(created),
        output_tokens: wire.completion_tokens,
        cache_read_tokens: read,
        cache_creation_tokens: created,
    }
}
