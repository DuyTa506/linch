//! llama.cpp, checked on 2026-11-06 against `tools/server/README.md` and
//! the source at master 67107ac3
//! (<https://github.com/ggml-org/llama.cpp/tree/master/tools/server>):
//! - **Reasoning** streams in `delta.reasoning_content` (the default
//!   `--reasoning-format`); assistant turns accept `reasoning_content` back.
//! - **Thinking off:** `chat_template_kwargs` `enable_thinking: false`, the
//!   README's own example; the server reads that flag itself.
//! - **`reasoning_effort`** is accepted and passed to the template.
//!   Checked 2026-11-17 in `tools/server/server-common.cpp` at master: the
//!   server answers 400 for a value the chat template lists as rejected
//!   and passes any other to it as `reasoning_effort`, so the model's
//!   template decides. Linch sends its level as is.
//! - **`tool_choice`:** `auto`, `none` and `required` only. A named
//!   function silently becomes `auto`, so it fails locally instead.
//!   Rechecked 2026-11-17 in the source at master: `common/chat.cpp`
//!   (`common_chat_tool_choice_parse_oaicompat`) parses only those three
//!   strings; a named object is not honoured (by a reading of
//!   `tools/server/server-common.cpp` it falls back to `auto`, not checked
//!   against a live server).
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
//! - **`output_schema`** is refused in a request: the profile sends no
//!   `response_format`, as vLLM's must not (its guided decoding binds the
//!   whole answer to the schema, so the model never calls a tool), and
//!   this server was not checked live for it: the same rule is kept
//!   rather than assumed away.
//!   [`linch_core::Provider::native_output_schema`] is false and the
//!   capabilities say `output_schema: final_tool`, so a run with a schema
//!   offers its final tool instead.

use linch_core::JsonObject;
use serde_json::json;

use super::{kwargs, llamacpp_usage};
use crate::context::ContextRule;
use crate::models::OpenAiRow;
use crate::openai::{ChatProfile, ModelList, ToolChoiceSupport, effort_value};
use crate::openai_compat::stream::ReasoningField;
use linch_core::{PromptCache, Support};

pub(super) static PROFILE: ChatProfile = ChatProfile {
    id: "llamacpp",
    reasoning_field: Some(ReasoningField::ReasoningContent),
    echo_reasoning: true,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: false,
    },
    max_tokens_field: "max_tokens",
    json_schema: false,
    reasoning_effort: Some(effort_value),
    template_thinking_switch: true,
    usage: llamacpp_usage,
    context: ContextRule::Props,
    overflow: &crate::overflow::LLAMACPP,
    vision: Support::ModelDependent,
    streams_tool_arguments: true,
    prompt_cache: PromptCache::Automatic,
    cache_isolation: None,
    stop_sequences: true,
    models: ModelList {
        row: OpenAiRow {
            context: None,
            output: None,
            modalities: false,
        },
        props_context: true,
    },
};

pub(super) fn thinking_off_kwargs() -> JsonObject {
    kwargs(json!({"enable_thinking": false}))
}
