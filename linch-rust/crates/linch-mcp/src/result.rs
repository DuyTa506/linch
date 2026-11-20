//! Turning an MCP `CallToolResult` into a Linch tool result.

use linch_core::{JsonObject, JsonValue, ToolOutput, ToolOutputError, ToolResult};
use rmcp::model::{CallToolResult, ContentBlock, ResourceContents};
use serde_json::json;

use crate::codes;

/// Longest result text, in bytes: Python's `OUTPUT_CAP`.
const OUTPUT_CAP: usize = 30_000;

/// Most images kept in `metadata.images` from one result; an image after
/// that many is left out.
const MAX_IMAGES: usize = 8;

/// Most bytes kept in `metadata.images` from one result: the base64 data and
/// the MIME type of each image kept. An image that would pass it is left out.
const MAX_IMAGE_BYTES: usize = 4 * 1024 * 1024;

/// Longest MIME type kept with an image, in bytes (the limit of a media type
/// in RFC 6838 is 127 + 1 + 127); an image with a longer one is left out.
const MAX_MIME_BYTES: usize = 255;

/// `text`, cut at [`OUTPUT_CAP`] bytes on a character boundary with a note
/// when it was longer.
pub(crate) fn capped(mut text: String) -> String {
    if text.len() > OUTPUT_CAP {
        let mut cut = OUTPUT_CAP;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
        text.push_str(&format!("\n… (output capped at {OUTPUT_CAP} bytes)"));
    }
    text
}

/// Map one result: the text blocks become the output, images go to
/// `metadata.images`, and an `isError` result becomes a [`ToolOutputError`]
/// carrying the same text. The text is cut at [`OUTPUT_CAP`], and the images
/// kept are at most [`MAX_IMAGES`], with [`MAX_IMAGE_BYTES`] of data and MIME
/// types in all and a MIME type of at most [`MAX_MIME_BYTES`] each; the text
/// of a dropped image says which limit dropped it.
pub(crate) fn tool_result(result: CallToolResult) -> ToolResult {
    let mut parts = Vec::new();
    let mut images = Vec::new();
    let mut image_bytes = 0usize;
    for block in &result.content {
        parts.push(match block {
            ContentBlock::Text(text) => text.text.clone(),
            ContentBlock::Image(image) => {
                let size = image.data.len() + image.mime_type.len();
                if image.mime_type.len() > MAX_MIME_BYTES {
                    format!("[image omitted: MIME type over {MAX_MIME_BYTES} bytes]")
                } else if images.len() >= MAX_IMAGES {
                    format!("[image omitted: more than {MAX_IMAGES} images]")
                } else if image_bytes + size > MAX_IMAGE_BYTES {
                    "[image omitted: over 4 MiB of image data]".to_owned()
                } else {
                    image_bytes += size;
                    images.push(json!({"mime_type": image.mime_type, "data": image.data}));
                    "[image]".to_owned()
                }
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
    let text = capped(if parts.is_empty() {
        "(no content)".to_owned()
    } else {
        parts.join("\n")
    });

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
    fn images_are_bounded_in_count_and_total_size() {
        let data = "A".repeat(1024 * 1024);
        let many: Vec<ContentBlock> = (0..20)
            .map(|_| ContentBlock::image(data.clone(), "image/png"))
            .collect();
        let output = tool_result(CallToolResult::success(many)).unwrap();
        let kept = output.metadata["images"].as_array().unwrap();
        let bytes: usize = kept
            .iter()
            .map(|image| image["data"].as_str().unwrap().len())
            .sum();
        assert!(kept.len() <= 8, "{} images kept", kept.len());
        assert!(bytes <= 4 * 1024 * 1024, "{bytes} image bytes kept");
        assert!(!kept.is_empty(), "the first images are kept");
    }

    /// A kept image's MIME type is the server's text too: the bound counts it.
    #[test]
    fn the_images_metadata_is_bounded_whatever_the_mime_type() {
        let mime = "m".repeat(255);
        let many: Vec<ContentBlock> = (0..8)
            .map(|_| ContentBlock::image("A".repeat(600 * 1024), mime.clone()))
            .collect();
        let output = tool_result(CallToolResult::success(many)).unwrap();
        let size = serde_json::to_vec(&output.metadata).unwrap().len();
        assert!(
            size <= MAX_IMAGE_BYTES + 64 * 1024,
            "{size} bytes of metadata"
        );
        let long = ContentBlock::image("aGk=", "m".repeat(1024 * 1024));
        let output = tool_result(CallToolResult::success(vec![long])).unwrap();
        assert!(
            output.metadata.is_empty(),
            "an image with a 1 MiB MIME type"
        );
        let text = output.value.as_str().unwrap();
        assert!(text.contains("MIME type"), "{text}");
    }

    /// The note says which limit dropped the image: count or size.
    #[test]
    fn the_note_of_a_dropped_image_names_the_limit() {
        let small: Vec<ContentBlock> = (0..9)
            .map(|_| ContentBlock::image("aGk=", "image/png"))
            .collect();
        let output = tool_result(CallToolResult::success(small)).unwrap();
        let text = output.value.as_str().unwrap();
        assert!(
            text.ends_with("[image omitted: more than 8 images]"),
            "{text}"
        );
        let big = vec![
            ContentBlock::image("A".repeat(5 * 1024 * 1024), "image/png"),
            ContentBlock::image("aGk=", "image/png"),
        ];
        let output = tool_result(CallToolResult::success(big)).unwrap();
        let text = output.value.as_str().unwrap();
        assert!(
            text.starts_with("[image omitted: over 4 MiB of image data]"),
            "{text}"
        );
        assert_eq!(
            output.metadata["images"].as_array().map(Vec::len),
            Some(1),
            "a later small image is kept"
        );
    }

    #[test]
    fn capped_cuts_on_a_character_boundary() {
        let text = capped(format!("a{}", "\u{20ac}".repeat(20_000)));
        assert!(text.len() <= OUTPUT_CAP + 64);
        assert!(text.contains("output capped"));
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
