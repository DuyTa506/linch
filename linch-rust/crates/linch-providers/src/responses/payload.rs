//! Linch requests to the Responses API request body.

use linch_core::{
    ContentBlock, ImageSource, JsonValue, Message, ProviderError, ProviderRequest, Role,
    ToolChoice, ToolResultBlock, ToolResultPart,
};
use serde_json::json;

use super::ResponsesOptions;
use super::stream::METADATA_KEY;
use crate::openai::effort_value;

/// The request body for `request`.
pub(crate) fn build_payload(
    request: &ProviderRequest,
    options: &ResponsesOptions,
) -> Result<JsonValue, ProviderError> {
    if request.output_schema.is_some() {
        return Err(invalid(
            "the OpenAI Responses adapter does not support output_schema yet".into(),
        ));
    }
    if !request.stop_sequences.is_empty() {
        return Err(invalid(
            "the Responses API has no stop sequences; remove stop_sequences".into(),
        ));
    }
    let mut body = json!({
        "model": request.model,
        "input": input(&request.messages)?,
        "stream": true,
    });
    if !request.system.is_empty() {
        let text: Vec<&str> = request
            .system
            .iter()
            .map(|block| block.text.as_str())
            .collect();
        body["instructions"] = text.join("\n\n").into();
    }
    if !request.tools.is_empty() {
        let tools: Vec<JsonValue> = request
            .tools
            .iter()
            .map(|tool| {
                json!({
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": false,
                })
            })
            .collect();
        body["tools"] = tools.into();
    }
    if let Some(choice) = &request.tool_choice {
        body["tool_choice"] = match choice {
            ToolChoice::Auto {} => "auto".into(),
            ToolChoice::Disabled {} => "none".into(),
            ToolChoice::Required {} => "required".into(),
            ToolChoice::Tool { name } => json!({"type": "function", "name": name}),
        };
    }
    if let Some(max_tokens) = request.max_output_tokens {
        body["max_output_tokens"] = max_tokens.into();
    }
    if let Some(temperature) = request.temperature {
        body["temperature"] = temperature.into();
    }
    let mut reasoning = serde_json::Map::new();
    if let Some(effort) = request.effort {
        reasoning.insert("effort".into(), effort_value(effort).into());
    }
    if let Some(summary) = options.reasoning_summary {
        reasoning.insert("summary".into(), summary.wire().into());
    }
    if !reasoning.is_empty() {
        body["reasoning"] = reasoning.into();
    }
    if !options.store {
        body["store"] = false.into();
        body["include"] = json!(["reasoning.encrypted_content"]);
    }
    Ok(body)
}

/// The `input` items, in conversation order.
fn input(messages: &[Message]) -> Result<Vec<JsonValue>, ProviderError> {
    let mut out = Vec::with_capacity(messages.len());
    for message in messages {
        match message.role {
            Role::Assistant => assistant(message, &mut out)?,
            Role::User => user(message, &mut out)?,
        }
    }
    Ok(out)
}

/// A turn this adapter produced is replayed from its stored output items,
/// which keeps reasoning items next to the calls they led to. A stored call
/// the turn does not hold is left out: the output limit cut it off, so no
/// result answers it. Any other assistant turn is rebuilt from its blocks.
fn assistant(message: &Message, out: &mut Vec<JsonValue>) -> Result<(), ProviderError> {
    let stored = message
        .provider_metadata
        .as_ref()
        .and_then(|metadata| metadata.get(METADATA_KEY))
        .and_then(|entry| entry.get("output"))
        .and_then(JsonValue::as_array);
    if let Some(items) = stored {
        let held = |item: &&JsonValue| {
            item["type"] != "function_call"
                || message
                    .tool_uses()
                    .any(|call| item["call_id"] == call.id.as_str())
        };
        out.extend(items.iter().filter(held).cloned());
        return Ok(());
    }
    let mut text = String::new();
    let mut calls = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => text.push_str(&block.text),
            ContentBlock::CatalogChange(block) => text.push_str(&block.text),
            // Another vendor's reasoning means nothing here.
            ContentBlock::Thinking(_) | ContentBlock::RedactedThinking(_) => {}
            ContentBlock::ToolUse(call) => calls.push(json!({
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": JsonValue::Object(call.input.clone()).to_string(),
            })),
            ContentBlock::Image(_) | ContentBlock::ToolResult(_) => {
                return Err(misplaced("assistant", block));
            }
        }
    }
    // An assistant message without an item id is sent with string content,
    // the only id-less assistant shape the docs show.
    if !text.is_empty() {
        out.push(json!({"type": "message", "role": "assistant", "content": text}));
    }
    out.extend(calls);
    Ok(())
}

/// Tool results become `function_call_output` items ahead of the user's own
/// parts, so they directly follow the calls they answer.
fn user(message: &Message, out: &mut Vec<JsonValue>) -> Result<(), ProviderError> {
    let mut parts = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => parts.push(input_text(&block.text)),
            ContentBlock::CatalogChange(block) => parts.push(input_text(&block.text)),
            ContentBlock::Image(image) => parts.push(input_image(&image.source)),
            ContentBlock::ToolResult(result) => out.push(call_output(result)),
            ContentBlock::ToolUse(_)
            | ContentBlock::Thinking(_)
            | ContentBlock::RedactedThinking(_) => return Err(misplaced("user", block)),
        }
    }
    if !parts.is_empty() {
        out.push(json!({"type": "message", "role": "user", "content": parts}));
    }
    Ok(())
}

/// Text-only output goes as one string; output with images as parts.
fn call_output(result: &ToolResultBlock) -> JsonValue {
    let texts: Option<Vec<&str>> = result
        .content
        .iter()
        .map(|part| match part {
            ToolResultPart::Text(text) => Some(text.text.as_str()),
            ToolResultPart::Image(_) => None,
        })
        .collect();
    let output: JsonValue = match texts {
        Some(texts) => texts.join("\n").into(),
        None => result
            .content
            .iter()
            .map(|part| match part {
                ToolResultPart::Text(text) => input_text(&text.text),
                ToolResultPart::Image(image) => input_image(&image.source),
            })
            .collect::<Vec<_>>()
            .into(),
    };
    json!({"type": "function_call_output", "call_id": result.tool_use_id, "output": output})
}

fn input_text(text: &str) -> JsonValue {
    json!({"type": "input_text", "text": text})
}

/// `detail` is sent because the reference marks it required.
fn input_image(source: &ImageSource) -> JsonValue {
    let url = match source {
        ImageSource::Url { url } => url.clone(),
        ImageSource::Base64 { media_type, data } => format!("data:{media_type};base64,{data}"),
    };
    json!({"type": "input_image", "image_url": url, "detail": "auto"})
}

fn misplaced(role: &str, block: &ContentBlock) -> ProviderError {
    let kind = serde_json::to_value(block)
        .ok()
        .and_then(|value| value.get("type")?.as_str().map(String::from))
        .unwrap_or_default();
    invalid(format!("a {role} message cannot carry a {kind} block"))
}

fn invalid(message: String) -> ProviderError {
    ProviderError::InvalidRequest { message }
}
