//! A [`ProviderRequest`] to the Messages API request body.

use linch_core::provider::ThinkingDisplay;
use linch_core::{
    CacheControl, CacheTtl, ContentBlock, ImageSource, JsonValue, Message, ProviderError,
    ProviderRequest, ReasoningEffort, Role, SystemBlock, Thinking, ToolChoice, ToolDefinition,
    ToolResultBlock, ToolResultPart,
};
use serde_json::json;

use super::DEFAULT_MAX_TOKENS;

/// The streaming request body for `request`.
pub(crate) fn build(request: &ProviderRequest) -> Result<JsonValue, ProviderError> {
    if request.output_schema.is_some() {
        return Err(invalid(
            "the Anthropic adapter does not support output_schema yet",
        ));
    }
    let max_tokens = request.max_output_tokens.unwrap_or(DEFAULT_MAX_TOKENS);
    let manual_thinking = matches!(
        request.thinking,
        Some(Thinking::Enabled {
            budget_tokens: Some(_),
            ..
        })
    );
    if manual_thinking
        && matches!(
            request.tool_choice,
            Some(ToolChoice::Required {} | ToolChoice::Tool { .. })
        )
    {
        return Err(invalid(
            "Anthropic rejects a forced tool_choice with budgeted thinking; \
             use tool_choice auto, adaptive thinking, or no thinking",
        ));
    }
    let cache = request.cache_control.map(cache_block);
    let mut body = json!({
        "model": request.model,
        "max_tokens": max_tokens,
        "stream": true,
        "messages": messages(&request.messages, cache.as_ref())?,
    });
    let system = system(&request.system, cache.as_ref());
    if !system.is_empty() {
        body["system"] = system.into();
    }
    if !request.tools.is_empty() {
        body["tools"] = tools(&request.tools).into();
    }
    if let Some(choice) = &request.tool_choice {
        body["tool_choice"] = tool_choice(choice);
    }
    if let Some(temperature) = request.temperature {
        body["temperature"] = temperature.into();
    }
    if !request.stop_sequences.is_empty() {
        body["stop_sequences"] = request.stop_sequences.clone().into();
    }
    if let Some(effort) = request.effort {
        body["output_config"] = json!({"effort": effort_value(effort)});
    }
    if let Some(state) = &request.thinking {
        body["thinking"] = thinking(state, max_tokens)?;
    }
    Ok(body)
}

/// `None` sends no `thinking` field, so the model default applies.
fn thinking(state: &Thinking, max_tokens: u32) -> Result<JsonValue, ProviderError> {
    let (mut value, display) = match state {
        Thinking::Disabled {} => return Ok(json!({"type": "disabled"})),
        Thinking::Adaptive { display } => (json!({"type": "adaptive"}), display),
        // `enabled` requires a budget; with none the model picks its own
        // depth, which is what `adaptive` means.
        Thinking::Enabled {
            budget_tokens: None,
            display,
        } => (json!({"type": "adaptive"}), display),
        Thinking::Enabled {
            budget_tokens: Some(budget),
            display,
        } => {
            if *budget >= max_tokens {
                return Err(invalid(&format!(
                    "thinking budget_tokens ({budget}) must be below max_tokens ({max_tokens})"
                )));
            }
            (json!({"type": "enabled", "budget_tokens": budget}), display)
        }
    };
    if let Some(display) = display {
        value["display"] = match display {
            ThinkingDisplay::Summarized => "summarized",
            ThinkingDisplay::Omitted => "omitted",
        }
        .into();
    }
    Ok(value)
}

/// `{"type": "ephemeral"}`; five minutes is the default lifetime, so only
/// one hour is spelled out.
fn cache_block(control: CacheControl) -> JsonValue {
    match control.ttl {
        Some(CacheTtl::OneHour) => json!({"type": "ephemeral", "ttl": "1h"}),
        Some(CacheTtl::FiveMinutes) | None => json!({"type": "ephemeral"}),
    }
}

/// System text blocks; the cache breakpoint goes on the last cacheable
/// one, so a volatile trailing block stays outside the cached prefix.
fn system(blocks: &[SystemBlock], cache: Option<&JsonValue>) -> Vec<JsonValue> {
    let mut out = Vec::with_capacity(blocks.len());
    let mut boundary = None;
    for block in blocks.iter().filter(|block| !block.text.is_empty()) {
        if block.cacheable {
            boundary = Some(out.len());
        }
        out.push(text(&block.text));
    }
    if let (Some(cache), Some(index)) = (cache, boundary) {
        out[index]["cache_control"] = cache.clone();
    }
    out
}

/// The conversation; with caching on, the last block of the last user
/// message is the second breakpoint.
fn messages(
    conversation: &[Message],
    cache: Option<&JsonValue>,
) -> Result<Vec<JsonValue>, ProviderError> {
    let last_user = conversation
        .iter()
        .rposition(|message| message.role == Role::User);
    let mut out = Vec::with_capacity(conversation.len());
    for (index, message) in conversation.iter().enumerate() {
        let (role, mut content) = match message.role {
            Role::Assistant => ("assistant", assistant(message)?),
            Role::User => ("user", user(message)?),
        };
        if content.is_empty() {
            continue;
        }
        if let (Some(cache), true, Some(last)) =
            (cache, Some(index) == last_user, content.last_mut())
        {
            last["cache_control"] = cache.clone();
        }
        out.push(json!({"role": role, "content": content}));
    }
    Ok(out)
}

fn assistant(message: &Message) -> Result<Vec<JsonValue>, ProviderError> {
    let mut out = Vec::with_capacity(message.content.len());
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => push_text(&mut out, &block.text),
            ContentBlock::CatalogChange(block) => push_text(&mut out, &block.text),
            ContentBlock::ToolUse(call) => out.push(json!({
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.input,
            })),
            // The signature is what the API verifies, even when the text is
            // empty (display omitted). An unsigned block came from another
            // vendor and would be rejected.
            ContentBlock::Thinking(block) => {
                if let Some(signature) = &block.signature {
                    out.push(json!({
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": signature,
                    }));
                }
            }
            ContentBlock::RedactedThinking(block) => {
                out.push(json!({"type": "redacted_thinking", "data": block.data}));
            }
            ContentBlock::Image(_) | ContentBlock::ToolResult(_) => {
                return Err(misplaced("assistant", block));
            }
        }
    }
    Ok(out)
}

/// Tool results lead the message, in order, then the user's own blocks:
/// Anthropic requires the results right after the turn that called.
fn user(message: &Message) -> Result<Vec<JsonValue>, ProviderError> {
    let mut results = Vec::new();
    let mut parts = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::ToolResult(result) => results.push(tool_result(result)),
            ContentBlock::Text(block) => push_text(&mut parts, &block.text),
            ContentBlock::CatalogChange(block) => push_text(&mut parts, &block.text),
            ContentBlock::Image(image) => parts.push(image_block(&image.source)),
            ContentBlock::ToolUse(_)
            | ContentBlock::Thinking(_)
            | ContentBlock::RedactedThinking(_) => return Err(misplaced("user", block)),
        }
    }
    results.extend(parts);
    Ok(results)
}

fn tool_result(result: &ToolResultBlock) -> JsonValue {
    let mut content = Vec::with_capacity(result.content.len());
    for part in &result.content {
        match part {
            ToolResultPart::Text(block) => push_text(&mut content, &block.text),
            ToolResultPart::Image(image) => content.push(image_block(&image.source)),
        }
    }
    let mut item = json!({"type": "tool_result", "tool_use_id": result.tool_use_id});
    if !content.is_empty() {
        item["content"] = content.into();
    }
    if result.is_error {
        item["is_error"] = true.into();
    }
    item
}

fn text(text: &str) -> JsonValue {
    json!({"type": "text", "text": text})
}

/// The API rejects empty text blocks, so they are left out.
fn push_text(out: &mut Vec<JsonValue>, value: &str) {
    if !value.is_empty() {
        out.push(text(value));
    }
}

fn image_block(source: &ImageSource) -> JsonValue {
    let source = match source {
        ImageSource::Base64 { media_type, data } => {
            json!({"type": "base64", "media_type": media_type, "data": data})
        }
        ImageSource::Url { url } => json!({"type": "url", "url": url}),
    };
    json!({"type": "image", "source": source})
}

fn tools(tools: &[ToolDefinition]) -> Vec<JsonValue> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
        })
        .collect()
}

fn tool_choice(choice: &ToolChoice) -> JsonValue {
    match choice {
        ToolChoice::Auto {} => json!({"type": "auto"}),
        ToolChoice::Disabled {} => json!({"type": "none"}),
        ToolChoice::Required {} => json!({"type": "any"}),
        ToolChoice::Tool { name } => json!({"type": "tool", "name": name}),
    }
}

fn effort_value(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Low => "low",
        ReasoningEffort::Medium => "medium",
        ReasoningEffort::High => "high",
        ReasoningEffort::Xhigh => "xhigh",
        ReasoningEffort::Max => "max",
    }
}

fn misplaced(role: &str, block: &ContentBlock) -> ProviderError {
    let kind = serde_json::to_value(block)
        .ok()
        .and_then(|value| Some(value.get("type")?.as_str()?.to_string()))
        .unwrap_or_default();
    invalid(&format!("a {role} message cannot carry a {kind} block"))
}

fn invalid(message: &str) -> ProviderError {
    ProviderError::InvalidRequest {
        message: message.into(),
    }
}
