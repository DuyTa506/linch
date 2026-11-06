//! Turning an MCP `CallToolResult` into a Linch tool result.

use linch_core::{JsonObject, JsonValue, ToolOutput, ToolOutputError, ToolResult};
use rmcp::model::{CallToolResult, ContentBlock, ResourceContents};
use serde_json::json;

use crate::codes;

/// Longest result text, in bytes: Python's `OUTPUT_CAP`.
const OUTPUT_CAP: usize = 30_000;

/// Map one result: the text blocks become the output, images go to
/// `metadata.images`, and an `isError` result becomes a [`ToolOutputError`]
/// carrying the same text. The text is cut at [`OUTPUT_CAP`].
pub(crate) fn tool_result(result: CallToolResult) -> ToolResult {
    let mut parts = Vec::new();
    let mut images = Vec::new();
    for block in &result.content {
        parts.push(match block {
            ContentBlock::Text(text) => text.text.clone(),
            ContentBlock::Image(image) => {
                images.push(json!({"mime_type": image.mime_type, "data": image.data}));
                "[image]".to_owned()
            }
            ContentBlock::Audio(_) => "[audio content omitted]".to_owned(),
            ContentBlock::Resource(resource) => match &resource.resource {
                ResourceContents::TextResourceContents { text, .. } => text.clone(),
                _ => "[resource content omitted]".to_owned(),
            },
            ContentBlock::ResourceLink(link) => format!("[resource link: {}]", link.uri),
            _ => "[unsupported content omitted]".to_owned(),
        });
    }
    // The spec asks servers to mirror structured content in a text block;
    // fall back to it when a server did not.
    if parts.is_empty()
        && let Some(structured) = &result.structured_content
    {
        parts.push(structured.to_string());
    }
    let mut text = if parts.is_empty() {
        "(no content)".to_owned()
    } else {
        parts.join("\n")
    };
    if text.len() > OUTPUT_CAP {
        let mut cut = OUTPUT_CAP;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
        text.push_str(&format!("\n… (output capped at {OUTPUT_CAP} bytes)"));
    }

    if result.is_error == Some(true) {
        return Err(ToolOutputError {
            message: text,
            code: Some(codes::TOOL_ERROR.to_owned()),
            details: None,
        });
    }
    let mut metadata = JsonObject::new();
    if !images.is_empty() {
        metadata.insert("images".into(), JsonValue::Array(images));
    }
    Ok(ToolOutput {
        value: JsonValue::String(text),
        metadata,
    })
}

#[cfg(test)]
mod tests {
    use rmcp::model::{AudioContent, ContentBlock};

    use super::*;

    #[test]
    fn text_blocks_join_into_the_output() {
        let result =
            CallToolResult::success(vec![ContentBlock::text("one"), ContentBlock::text("two")]);
        let output = tool_result(result).unwrap();
        assert_eq!(output.value, json!("one\ntwo"));
        assert!(output.metadata.is_empty());
    }

    #[test]
    fn is_error_becomes_a_tool_error_with_the_text() {
        let result = CallToolResult::error(vec![ContentBlock::text("bad date")]);
        let error = tool_result(result).unwrap_err();
        assert_eq!(error.message, "bad date");
        assert_eq!(error.code.as_deref(), Some(codes::TOOL_ERROR));
    }

    #[test]
    fn images_are_metadata_and_other_blocks_are_placeholders() {
        let result = CallToolResult::success(vec![
            ContentBlock::text("chart:"),
            ContentBlock::image("aGk=", "image/png"),
            ContentBlock::Audio(AudioContent::new("aGk=", "audio/wav")),
            ContentBlock::embedded_text("file:///a.txt", "inside"),
        ]);
        let output = tool_result(result).unwrap();
        assert_eq!(
            output.value,
            json!("chart:\n[image]\n[audio content omitted]\ninside")
        );
        assert_eq!(
            output.metadata["images"],
            json!([{"mime_type": "image/png", "data": "aGk="}])
        );
    }

    #[test]
    fn long_text_is_capped_on_a_char_boundary() {
        // One byte, then two per char, so byte 30000 falls inside a char.
        let long = format!("a{}", "é".repeat(20_000));
        let output = tool_result(CallToolResult::success(vec![ContentBlock::text(
            long.clone(),
        )]))
        .unwrap();
        let text = output.value.as_str().unwrap();
        let note = "\n… (output capped at 30000 bytes)";
        assert_eq!(text, format!("a{}{note}", "é".repeat(14_999)));
        let error = tool_result(CallToolResult::error(vec![ContentBlock::text(long)])).unwrap_err();
        assert!(error.message.ends_with(note), "{}", error.message);
        assert!(error.message.len() <= 30_000 + note.len());
    }

    #[test]
    fn empty_results_fall_back_to_structured_content_or_a_marker() {
        let mut structured_only = CallToolResult::success(vec![]);
        structured_only.structured_content = Some(json!({"n": 1}));
        assert_eq!(
            tool_result(structured_only).unwrap().value,
            json!("{\"n\":1}")
        );

        let empty = CallToolResult::success(vec![]);
        assert_eq!(tool_result(empty).unwrap().value, json!("(no content)"));
    }
}
