//! A [`ProviderRequest`] to the `generateContent` request body.

use linch_core::provider::ThinkingDisplay;
use linch_core::{
    ContentBlock, ImageSource, JsonValue, Message, ProviderError, ProviderRequest, ReasoningEffort,
    Role, Thinking, ToolChoice, ToolDefinition, ToolResultBlock, ToolResultPart,
};
use serde_json::{Map, json};

use super::PARTS_KEY;

/// The request body for `request`.
pub(crate) fn build(request: &ProviderRequest) -> Result<JsonValue, ProviderError> {
    if request.output_schema.is_some() {
        return Err(invalid(
            "the Gemini adapter does not support output_schema yet",
        ));
    }
    let mut body = json!({"contents": contents(&request.messages)?});
    let system: Vec<JsonValue> = request
        .system
        .iter()
        .filter(|block| !block.text.is_empty())
        .map(|block| json!({"text": block.text}))
        .collect();
    if !system.is_empty() {
        body["systemInstruction"] = json!({"parts": system});
    }
    if !request.tools.is_empty() {
        body["tools"] = json!([{"functionDeclarations": declarations(&request.tools)}]);
        if let Some(choice) = &request.tool_choice {
            body["toolConfig"] = json!({"functionCallingConfig": calling_config(choice)});
        }
    }
    let mut config = Map::new();
    if let Some(max_tokens) = request.max_output_tokens {
        config.insert("maxOutputTokens".into(), max_tokens.into());
    }
    if let Some(temperature) = request.temperature {
        config.insert("temperature".into(), temperature.into());
    }
    if !request.stop_sequences.is_empty() {
        config.insert(
            "stopSequences".into(),
            request.stop_sequences.clone().into(),
        );
    }
    if let Some(thinking) = thinking_config(request)? {
        config.insert("thinkingConfig".into(), thinking);
    }
    if !config.is_empty() {
        body["generationConfig"] = config.into();
    }
    Ok(body)
}

/// `thinkingBudget` only when the request names one (or `0` to turn
/// thinking off); Gemini 3 and later reject or mishandle budgets, so an
/// unbudgeted request leaves the model default. Effort becomes
/// `thinkingLevel`, which cannot be combined with a budget.
fn thinking_config(request: &ProviderRequest) -> Result<Option<JsonValue>, ProviderError> {
    let (budget, display) = match &request.thinking {
        None => (None, None),
        Some(Thinking::Disabled {}) => (Some(0), None),
        Some(Thinking::Adaptive { display }) => (None, *display),
        Some(Thinking::Enabled {
            budget_tokens,
            display,
        }) => (*budget_tokens, *display),
    };
    let mut config = Map::new();
    if let Some(budget) = budget {
        config.insert("thinkingBudget".into(), budget.into());
    }
    if let Some(display) = display {
        config.insert(
            "includeThoughts".into(),
            (display != ThinkingDisplay::Omitted).into(),
        );
    }
    let disabled = matches!(request.thinking, Some(Thinking::Disabled {}));
    if let (Some(effort), false) = (request.effort, disabled) {
        if budget.is_some() {
            return Err(invalid(
                "Gemini cannot combine a thinking budget with an effort level; set one",
            ));
        }
        config.insert("thinkingLevel".into(), level(effort).into());
    }
    Ok((!config.is_empty()).then(|| config.into()))
}

fn level(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Low => "LOW",
        ReasoningEffort::Medium => "MEDIUM",
        ReasoningEffort::High | ReasoningEffort::Xhigh | ReasoningEffort::Max => "HIGH",
    }
}

fn declarations(tools: &[ToolDefinition]) -> Vec<JsonValue> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "name": tool.name,
                "description": tool.description,
                "parametersJsonSchema": tool.input_schema,
            })
        })
        .collect()
}

fn calling_config(choice: &ToolChoice) -> JsonValue {
    match choice {
        ToolChoice::Auto {} => json!({"mode": "AUTO"}),
        ToolChoice::Disabled {} => json!({"mode": "NONE"}),
        ToolChoice::Required {} => json!({"mode": "ANY"}),
        ToolChoice::Tool { name } => json!({"mode": "ANY", "allowedFunctionNames": [name]}),
    }
}

/// One call of the assistant turn that tool results answer.
struct Call<'a> {
    id: &'a str,
    name: &'a str,
    /// The id Gemini gave the call, echoed in its response; synthesized
    /// ids never go on the wire.
    wire_id: Option<&'a str>,
}

/// Consecutive messages of one role go out as one turn. A compacted view
/// opens with two user messages, and Gemini is reported to answer two
/// user turns before a function call with a turn-order 400; AI Studio
/// merges such turns the same way.
fn contents(conversation: &[Message]) -> Result<Vec<JsonValue>, ProviderError> {
    let mut out: Vec<JsonValue> = Vec::with_capacity(conversation.len());
    let mut calls: Vec<Call<'_>> = Vec::new();
    for message in conversation {
        let (role, parts) = match message.role {
            Role::Assistant => {
                calls = calls_of(message);
                ("model", model_parts(message)?)
            }
            Role::User => ("user", user_parts(message, &calls)?),
        };
        if parts.is_empty() {
            continue;
        }
        if let Some(last) = out.last_mut().filter(|last| last["role"] == role)
            && let Some(previous) = last["parts"].as_array_mut()
        {
            previous.extend(parts);
        } else {
            out.push(json!({"role": role, "parts": parts}));
        }
    }
    Ok(out)
}

/// The raw parts Gemini sent, when the adapter kept them for their
/// thought signatures or call ids.
fn kept_parts(message: &Message) -> Option<&Vec<JsonValue>> {
    message
        .provider_metadata
        .as_ref()?
        .get(PARTS_KEY)?
        .as_array()
}

/// A turn Gemini produced is replayed exactly as received, so every
/// signature stays inside its original part.
fn model_parts(message: &Message) -> Result<Vec<JsonValue>, ProviderError> {
    if let Some(parts) = kept_parts(message) {
        return Ok(parts.clone());
    }
    let mut out = Vec::with_capacity(message.content.len());
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => push_text(&mut out, &block.text),
            ContentBlock::CatalogChange(block) => push_text(&mut out, &block.text),
            ContentBlock::ToolUse(call) => out.push(json!({
                "functionCall": {"name": call.name, "args": call.input},
            })),
            // Reasoning travels in signatures; another vendor's means
            // nothing here.
            ContentBlock::Thinking(_) | ContentBlock::RedactedThinking(_) => {}
            ContentBlock::Image(_) | ContentBlock::ToolResult(_) => {
                return Err(misplaced("assistant", block));
            }
        }
    }
    Ok(out)
}

fn calls_of(message: &Message) -> Vec<Call<'_>> {
    let wire_ids: Vec<Option<&str>> = kept_parts(message)
        .into_iter()
        .flatten()
        .filter_map(|part| part.get("functionCall"))
        .map(|call| call.get("id").and_then(JsonValue::as_str))
        .collect();
    message
        .tool_uses()
        .enumerate()
        .map(|(index, call)| Call {
            id: &call.id,
            name: &call.name,
            wire_id: wire_ids.get(index).copied().flatten(),
        })
        .collect()
}

/// Function responses lead the turn, in order, then the user's own parts.
fn user_parts(message: &Message, calls: &[Call<'_>]) -> Result<Vec<JsonValue>, ProviderError> {
    let mut results = Vec::new();
    let mut parts = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::ToolResult(result) => {
                let Some(call) = calls.iter().find(|call| call.id == result.tool_use_id) else {
                    return Err(invalid(&format!(
                        "tool result {:?} answers no call of the previous assistant turn",
                        result.tool_use_id
                    )));
                };
                results.push(function_response(result, call));
            }
            ContentBlock::Text(block) => push_text(&mut parts, &block.text),
            ContentBlock::CatalogChange(block) => push_text(&mut parts, &block.text),
            ContentBlock::Image(image) => parts.push(media(&image.source)),
            ContentBlock::ToolUse(_)
            | ContentBlock::Thinking(_)
            | ContentBlock::RedactedThinking(_) => return Err(misplaced("user", block)),
        }
    }
    results.extend(parts);
    Ok(results)
}

/// `response` carries the text under `output`, or `error` for a failed
/// call; images ride in the response's own `parts`.
fn function_response(result: &ToolResultBlock, call: &Call<'_>) -> JsonValue {
    let mut texts = Vec::new();
    let mut media_parts = Vec::new();
    for part in &result.content {
        match part {
            ToolResultPart::Text(block) => texts.push(block.text.as_str()),
            ToolResultPart::Image(image) => media_parts.push(media(&image.source)),
        }
    }
    let key = if result.is_error { "error" } else { "output" };
    let mut response = json!({"name": call.name, "response": {key: texts.join("\n")}});
    if let Some(id) = call.wire_id {
        response["id"] = id.into();
    }
    if !media_parts.is_empty() {
        response["parts"] = media_parts.into();
    }
    json!({"functionResponse": response})
}

fn push_text(out: &mut Vec<JsonValue>, text: &str) {
    if !text.is_empty() {
        out.push(json!({"text": text}));
    }
}

fn media(source: &ImageSource) -> JsonValue {
    match source {
        ImageSource::Base64 { media_type, data } => {
            json!({"inlineData": {"mimeType": media_type, "data": data}})
        }
        ImageSource::Url { url } => json!({"fileData": {"fileUri": url}}),
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
