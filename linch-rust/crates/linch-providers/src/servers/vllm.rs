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
//!   template; any value but `none` sets `enable_thinking`. The template
//!   decides what it accepts: the Qwen3 one (checked live 2026-11-17 on
//!   `qwen3.6-35b-a3b`) takes only `low`, `medium` and `xhigh` and answers
//!   HTTP 400 for `high` and `max`, so Linch sends `high` and `max` as
//!   `xhigh`. A model whose template takes other values is not served by
//!   this map.
//! - **`tool_choice`:** `auto`, `none`, `required` and a named function.
//!   In practice all but `none` need `--enable-auto-tool-choice` and
//!   `--tool-call-parser`; without them the server answers 400.
//!   Rechecked 2026-11-17 against
//!   <https://docs.vllm.ai/en/latest/features/tool_calling/>: named,
//!   `auto`, `required` (vLLM 0.8.3 and later) and `none`.
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
//! - **`output_schema`** is refused in a request: the profile sends no
//!   `response_format`. The server takes one, but with it and tools in the
//!   same request its guided decoding binds the whole answer to the
//!   schema, and the model never calls a tool: a request that asked for a
//!   tool call still came back as schema JSON with zero tool calls
//!   (checked by hand against a live vLLM server, 2026-11-17).
//!   [`linch_core::Provider::native_output_schema`] is false and the
//!   capabilities say `output_schema: final_tool`, so a run with a schema
//!   offers its final tool instead.

use linch_core::{JsonObject, ReasoningEffort, Usage};
use serde_json::json;

use super::kwargs;
use crate::context::ContextRule;
use crate::models::OpenAiRow;
use crate::openai::{ChatProfile, ModelList, ToolChoiceSupport};
use crate::openai_compat::stream::{ReasoningField, WireUsage};
use linch_core::{PromptCache, Support};

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "vllm",
    reasoning_field: Some(ReasoningField::Reasoning),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: true,
    },
    max_tokens_field: "max_completion_tokens",
    json_schema: false,
    reasoning_effort: Some(effort),
    template_thinking_switch: true,
    usage,
    context: ContextRule::Models,
    overflow: &crate::overflow::VLLM,
    vision: Support::ModelDependent,
    streams_tool_arguments: true,
    prompt_cache: PromptCache::Automatic,
    cache_isolation: Some("cache_salt"),
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
    kwargs(json!({"enable_thinking": false}))
}

/// The efforts vLLM's chat template takes: `low`, `medium` and `xhigh`.
/// `high` rounds up to `xhigh`, and `max` has no level above it.
fn effort(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Low => "low",
        ReasoningEffort::Medium => "medium",
        ReasoningEffort::High | ReasoningEffort::Xhigh | ReasoningEffort::Max => "xhigh",
    }
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
