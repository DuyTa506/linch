//! What one Chat Completions target supports, and the request body built
//! from it.
//!
//! A [`ChatProfile`] has no defaults: every target spells out every rule,
//! so a server profile never inherits OpenAI's behavior by accident.

use linch_core::provider::Thinking;
use linch_core::{
    JsonObject, JsonValue, ProviderError, ProviderRequest, ReasoningEffort, ToolChoice, Usage,
};
use serde_json::json;

use crate::openai_compat::payload;
use crate::openai_compat::stream::{ReasoningField, WireUsage};

/// The wire rules of one Chat Completions target.
#[derive(Debug)]
pub(crate) struct ChatProfile {
    /// [`linch_core::Provider::id`].
    pub id: &'static str,
    /// The delta field that streams reasoning text, if any.
    pub reasoning_field: Option<ReasoningField>,
    /// Whether earlier reasoning goes back as `reasoning_content` on
    /// assistant turns.
    pub echo_reasoning: bool,
    /// The `tool_choice` forms the target accepts.
    pub tool_choice: ToolChoiceSupport,
    /// The field that carries [`ProviderRequest::max_output_tokens`].
    pub max_tokens_field: &'static str,
    /// Whether [`ProviderRequest::effort`] is sent as `reasoning_effort`.
    pub reasoning_effort: bool,
    /// Whether [`Thinking::Disabled`] sends the configured
    /// `chat_template_kwargs`.
    pub template_thinking_switch: bool,
    /// Usage normalizer for the target's `usage` object.
    pub usage: fn(&WireUsage) -> Usage,
}

/// Which forced `tool_choice` forms a target accepts; `auto` and `none`
/// are accepted everywhere.
#[derive(Debug, Clone, Copy)]
pub(crate) struct ToolChoiceSupport {
    /// `"required"`.
    pub required: bool,
    /// `{"type": "function", "function": {"name": ...}}`.
    pub named: bool,
}

/// OpenAI's own Chat Completions endpoint; see the [`crate::openai`] docs.
pub(crate) static OPENAI: ChatProfile = ChatProfile {
    id: "openai-chat",
    reasoning_field: None,
    echo_reasoning: false,
    tool_choice: ToolChoiceSupport {
        required: true,
        named: true,
    },
    max_tokens_field: "max_completion_tokens",
    reasoning_effort: true,
    template_thinking_switch: false,
    usage: openai_usage,
};

/// OpenAI's effort values (`none` and `minimal` have no Linch level).
pub(crate) fn effort_value(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Low => "low",
        ReasoningEffort::Medium => "medium",
        ReasoningEffort::High => "high",
        ReasoningEffort::Xhigh => "xhigh",
        ReasoningEffort::Max => "max",
    }
}

/// `prompt_tokens` includes cache reads and cache writes; Linch counts
/// them apart. `completion_tokens` already includes reasoning tokens.
pub(crate) fn openai_usage(wire: &WireUsage) -> Usage {
    let details = wire.prompt_tokens_details.as_ref();
    let read = details
        .and_then(|details| details.cached_tokens)
        .unwrap_or(0);
    let written = details
        .and_then(|details| details.cache_write_tokens)
        .unwrap_or(0);
    Usage {
        input_tokens: wire
            .prompt_tokens
            .saturating_sub(read)
            .saturating_sub(written),
        output_tokens: wire.completion_tokens,
        cache_read_tokens: read,
        cache_creation_tokens: written,
    }
}

/// The request body for `request` under `profile`. `thinking_off` is the
/// `chat_template_kwargs` object sent with [`Thinking::Disabled`] when the
/// profile has a template switch; effort is then left out.
pub(crate) fn build_payload(
    profile: &ChatProfile,
    thinking_off: &JsonObject,
    request: &ProviderRequest,
) -> Result<JsonValue, ProviderError> {
    if request.output_schema.is_some() {
        return Err(invalid(format!(
            "the {} adapter does not support output_schema yet",
            profile.id
        )));
    }
    let mut body = json!({
        "model": request.model,
        "messages": payload::messages(&request.system, &request.messages, profile.echo_reasoning)?,
        "stream": true,
        "stream_options": {"include_usage": true},
    });
    if !request.tools.is_empty() {
        body["tools"] = payload::tools(&request.tools).into();
    }
    if let Some(choice) = &request.tool_choice {
        check_tool_choice(profile, choice)?;
        body["tool_choice"] = payload::tool_choice(choice);
    }
    if let Some(max_tokens) = request.max_output_tokens {
        body[profile.max_tokens_field] = max_tokens.into();
    }
    if let Some(temperature) = request.temperature {
        body["temperature"] = temperature.into();
    }
    if !request.stop_sequences.is_empty() {
        body["stop"] = request.stop_sequences.clone().into();
    }
    let switched_off =
        profile.template_thinking_switch && matches!(request.thinking, Some(Thinking::Disabled {}));
    if switched_off {
        // Effort would turn thinking back on in some templates, so it is
        // not sent with the switch.
        if !thinking_off.is_empty() {
            body["chat_template_kwargs"] = thinking_off.clone().into();
        }
    } else if let (true, Some(effort)) = (profile.reasoning_effort, request.effort) {
        body["reasoning_effort"] = effort_value(effort).into();
    }
    Ok(body)
}

fn check_tool_choice(profile: &ChatProfile, choice: &ToolChoice) -> Result<(), ProviderError> {
    let supported = match choice {
        ToolChoice::Auto {} | ToolChoice::Disabled {} => true,
        ToolChoice::Required {} => profile.tool_choice.required,
        ToolChoice::Tool { .. } => profile.tool_choice.named,
    };
    if supported {
        return Ok(());
    }
    Err(invalid(format!(
        "{} does not support this tool_choice; use auto or none",
        profile.id
    )))
}

fn invalid(message: String) -> ProviderError {
    ProviderError::InvalidRequest { message }
}
