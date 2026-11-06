//! Linch messages and tools to the Chat Completions request shape.

use linch_core::{
    ContentBlock, ImageSource, JsonValue, Message, ProviderError, Role, SystemBlock, ToolChoice,
    ToolDefinition, ToolResultBlock, ToolResultPart,
};
use serde_json::json;

/// Sent in place of an empty tool result so the tool turn is never blank.
const EMPTY_TOOL_RESULT: &str = "[tool result]";

/// The `messages` array: one system message, then the conversation.
///
/// With `echo_reasoning`, an assistant turn's thinking blocks go back as
/// `reasoning_content`; DeepSeek requires that on every assistant turn of a
/// request that offers tools.
pub(crate) fn messages(
    system: &[SystemBlock],
    conversation: &[Message],
    echo_reasoning: bool,
) -> Result<Vec<JsonValue>, ProviderError> {
    let mut out = Vec::with_capacity(conversation.len() + 1);
    if !system.is_empty() {
        let text: Vec<&str> = system.iter().map(|block| block.text.as_str()).collect();
        out.push(json!({"role": "system", "content": text.join("\n\n")}));
    }
    for message in conversation {
        match message.role {
            Role::Assistant => out.push(assistant(message, echo_reasoning)?),
            Role::User => user(message, &mut out)?,
        }
    }
    Ok(out)
}

fn assistant(message: &Message, echo_reasoning: bool) -> Result<JsonValue, ProviderError> {
    let mut text = String::new();
    let mut reasoning: Option<String> = None;
    let mut calls = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => text.push_str(&block.text),
            ContentBlock::CatalogChange(block) => text.push_str(&block.text),
            ContentBlock::Thinking(block) => {
                reasoning.get_or_insert_default().push_str(&block.thinking);
            }
            // Another vendor's encrypted reasoning means nothing here.
            ContentBlock::RedactedThinking(_) => {}
            ContentBlock::ToolUse(call) => calls.push(json!({
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": JsonValue::Object(call.input.clone()).to_string(),
                },
            })),
            ContentBlock::Image(_) | ContentBlock::ToolResult(_) => {
                return Err(misplaced("assistant", block));
            }
        }
    }
    let mut entry = json!({
        "role": "assistant",
        "content": if text.is_empty() { JsonValue::Null } else { text.into() },
    });
    if let (true, Some(reasoning)) = (echo_reasoning, reasoning) {
        entry["reasoning_content"] = reasoning.into();
    }
    if !calls.is_empty() {
        entry["tool_calls"] = calls.into();
    }
    Ok(entry)
}

/// Tool results become `role: tool` messages, in order, ahead of the
/// user's own parts, so they directly follow the assistant's calls.
fn user(message: &Message, out: &mut Vec<JsonValue>) -> Result<(), ProviderError> {
    let mut parts = Vec::new();
    for block in &message.content {
        match block {
            ContentBlock::Text(block) => parts.push(text_part(&block.text)),
            ContentBlock::CatalogChange(block) => parts.push(text_part(&block.text)),
            ContentBlock::Image(image) => parts.push(image_part(&image.source)),
            ContentBlock::ToolResult(result) => out.push(tool_message(result)),
            ContentBlock::ToolUse(_)
            | ContentBlock::Thinking(_)
            | ContentBlock::RedactedThinking(_) => return Err(misplaced("user", block)),
        }
    }
    if !parts.is_empty() {
        out.push(json!({"role": "user", "content": parts}));
    }
    Ok(())
}

/// Text-only results go as one string; results with images as parts.
fn tool_message(result: &ToolResultBlock) -> JsonValue {
    let texts: Option<Vec<&str>> = result
        .content
        .iter()
        .map(|part| match part {
            ToolResultPart::Text(text) => Some(text.text.as_str()),
            ToolResultPart::Image(_) => None,
        })
        .collect();
    let content: JsonValue = match texts {
        Some(texts) if texts.iter().all(|text| text.is_empty()) => EMPTY_TOOL_RESULT.into(),
        Some(texts) => texts.join("\n").into(),
        None => result
            .content
            .iter()
            .map(|part| match part {
                ToolResultPart::Text(text) => text_part(&text.text),
                ToolResultPart::Image(image) => image_part(&image.source),
            })
            .collect::<Vec<_>>()
            .into(),
    };
    json!({"role": "tool", "tool_call_id": result.tool_use_id, "content": content})
}

fn text_part(text: &str) -> JsonValue {
    json!({"type": "text", "text": text})
}

fn image_part(source: &ImageSource) -> JsonValue {
    let url = match source {
        ImageSource::Url { url } => url.clone(),
        ImageSource::Base64 { media_type, data } => format!("data:{media_type};base64,{data}"),
    };
    json!({"type": "image_url", "image_url": {"url": url}})
}

fn misplaced(role: &str, block: &ContentBlock) -> ProviderError {
    let kind = serde_json::to_value(block)
        .ok()
        .and_then(|value| {
            value
                .get("type")
                .and_then(JsonValue::as_str)
                .map(String::from)
        })
        .unwrap_or_default();
    ProviderError::InvalidRequest {
        message: format!("a {role} message cannot carry a {kind} block"),
    }
}

/// The `tools` array of function tools.
pub(crate) fn tools(tools: &[ToolDefinition]) -> Vec<JsonValue> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        })
        .collect()
}

/// The `tool_choice` value.
pub(crate) fn tool_choice(choice: &ToolChoice) -> JsonValue {
    match choice {
        ToolChoice::Auto {} => "auto".into(),
        ToolChoice::Disabled {} => "none".into(),
        ToolChoice::Required {} => "required".into(),
        ToolChoice::Tool { name } => json!({"type": "function", "function": {"name": name}}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use linch_core::{ImageBlock, TextBlock, ThinkingBlock, ToolUseBlock};

    fn call(id: &str) -> ContentBlock {
        ContentBlock::ToolUse(ToolUseBlock {
            id: id.into(),
            name: "get_weather".into(),
            input: json!({"city": "Paris"}).as_object().unwrap().clone(),
        })
    }

    #[test]
    fn system_blocks_become_one_system_message() {
        let system = [
            SystemBlock {
                text: "a".into(),
                cacheable: true,
            },
            SystemBlock {
                text: "b".into(),
                cacheable: false,
            },
        ];
        let out = messages(&system, &[Message::user_text("hi")], true).unwrap();
        assert_eq!(out[0], json!({"role": "system", "content": "a\n\nb"}));
        assert_eq!(
            out[1],
            json!({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        );
        assert_eq!(messages(&[], &[], true).unwrap(), Vec::<JsonValue>::new());
    }

    #[test]
    fn assistant_tool_turn_carries_calls_and_reasoning() {
        let turn = Message::new(
            Role::Assistant,
            vec![
                ContentBlock::Thinking(ThinkingBlock {
                    thinking: "need weather".into(),
                    signature: None,
                }),
                call("c1"),
            ],
        );
        let out = messages(&[], std::slice::from_ref(&turn), true).unwrap();
        assert_eq!(
            out[0],
            json!({
                "role": "assistant",
                "content": null,
                "reasoning_content": "need weather",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"},
                }],
            })
        );
        let plain = messages(&[], &[turn], false).unwrap();
        assert!(plain[0].get("reasoning_content").is_none());
    }

    #[test]
    fn assistant_text_turn_without_thinking_has_no_reasoning_field() {
        let out = messages(&[], &[Message::assistant_text("done")], true).unwrap();
        assert_eq!(out[0], json!({"role": "assistant", "content": "done"}));
    }

    #[test]
    fn tool_results_become_tool_messages_before_user_text() {
        let turn = Message::new(
            Role::User,
            vec![
                ContentBlock::text("and then?"),
                ContentBlock::ToolResult(ToolResultBlock::text("c1", "18C", false)),
                ContentBlock::ToolResult(ToolResultBlock {
                    tool_use_id: "c2".into(),
                    content: vec![],
                    is_error: false,
                }),
            ],
        );
        let out = messages(&[], &[turn], true).unwrap();
        assert_eq!(
            out,
            vec![
                json!({"role": "tool", "tool_call_id": "c1", "content": "18C"}),
                json!({"role": "tool", "tool_call_id": "c2", "content": "[tool result]"}),
                json!({"role": "user", "content": [{"type": "text", "text": "and then?"}]}),
            ]
        );
    }

    #[test]
    fn images_become_image_url_parts() {
        let image = ImageBlock {
            source: ImageSource::Base64 {
                media_type: "image/png".into(),
                data: "AAAA".into(),
            },
        };
        let result = ToolResultBlock {
            tool_use_id: "c1".into(),
            content: vec![
                ToolResultPart::Text(TextBlock { text: "see".into() }),
                ToolResultPart::Image(image.clone()),
            ],
            is_error: false,
        };
        let out = messages(
            &[],
            &[Message::new(
                Role::User,
                vec![ContentBlock::ToolResult(result), ContentBlock::Image(image)],
            )],
            true,
        )
        .unwrap();
        let url = json!({"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}});
        assert_eq!(
            out[0]["content"],
            json!([{"type": "text", "text": "see"}, url])
        );
        assert_eq!(out[1]["content"], json!([url]));
    }

    #[test]
    fn misplaced_blocks_are_rejected() {
        let user_call = Message::new(Role::User, vec![call("c1")]);
        let err = messages(&[], &[user_call], true).unwrap_err();
        assert!(
            matches!(&err, ProviderError::InvalidRequest { message } if message.contains("tool_use")),
            "{err}"
        );
        let assistant_result = Message::new(
            Role::Assistant,
            vec![ContentBlock::ToolResult(ToolResultBlock::text(
                "c1", "x", false,
            ))],
        );
        assert!(messages(&[], &[assistant_result], true).is_err());
    }

    #[test]
    fn tools_and_tool_choice_use_function_shape() {
        let tool = ToolDefinition {
            name: "get_weather".into(),
            description: "Weather.".into(),
            input_schema: json!({"type": "object"}).as_object().unwrap().clone(),
        };
        assert_eq!(
            tools(&[tool]),
            vec![json!({"type": "function", "function": {
                "name": "get_weather", "description": "Weather.", "parameters": {"type": "object"}
            }})]
        );
        assert_eq!(tool_choice(&ToolChoice::Auto {}), json!("auto"));
        assert_eq!(tool_choice(&ToolChoice::Disabled {}), json!("none"));
        assert_eq!(tool_choice(&ToolChoice::Required {}), json!("required"));
        assert_eq!(
            tool_choice(&ToolChoice::Tool { name: "x".into() }),
            json!({"type": "function", "function": {"name": "x"}})
        );
    }
}
