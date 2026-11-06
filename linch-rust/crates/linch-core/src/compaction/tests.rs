use serde_json::json;

use super::*;
use crate::message::{ImageBlock, ImageSource, ToolResultBlock, ToolUseBlock};

fn tool_turn(i: usize) -> [Message; 2] {
    let call = ToolUseBlock {
        id: format!("c{i}"),
        name: "Read".into(),
        input: json!({"path": "a"}).as_object().unwrap().clone(),
    };
    let result = ToolResultBlock::text(format!("c{i}"), "text", false);
    [
        Message::new(Role::Assistant, vec![ContentBlock::ToolUse(call)]),
        Message::new(Role::User, vec![ContentBlock::ToolResult(result)]),
    ]
}

/// The prompt, then `n` tool turns.
fn view(n: usize) -> Vec<Arc<Message>> {
    let mut messages = vec![Message::user_text("go")];
    messages.extend((0..n).flat_map(tool_turn));
    messages.into_iter().map(Arc::new).collect()
}

#[test]
fn a_spec_reads_strictly_with_defaults() {
    let spec: CompactionSpec = serde_json::from_value(json!({"threshold_tokens": 9})).unwrap();
    assert_eq!(spec, CompactionSpec::new(9));
    assert_eq!((spec.keep_recent_turns, spec.max_output_tokens), (10, 4096));
    assert_eq!(spec.problem(), None);
    let extra = json!({"threshold_tokens": 9, "strategy": "micro"});
    assert!(serde_json::from_value::<CompactionSpec>(extra).is_err());
    assert!(CompactionSpec::new(0).problem().is_some());
    let none_kept = CompactionSpec {
        keep_recent_turns: 0,
        ..CompactionSpec::new(1)
    };
    assert!(none_kept.problem().is_some());
}

#[test]
fn the_estimate_counts_text_and_tool_data_but_not_opaque_payloads() {
    assert_eq!(estimate_tokens(&[Arc::new(Message::user_text("abcd"))]), 1);
    let image = ContentBlock::Image(ImageBlock {
        source: ImageSource::Url {
            url: "x".repeat(4000),
        },
    });
    let redacted = ContentBlock::RedactedThinking(crate::message::RedactedThinkingBlock {
        data: "x".repeat(4000),
    });
    let opaque = Message::new(Role::User, vec![image, redacted]);
    assert_eq!(estimate_tokens(&[Arc::new(opaque)]), 1000);
    // "Read" (4) + {"path":"a"} (12) + "text" (4).
    assert_eq!(estimate_tokens(&view(1)[1..]), 5);
}

#[test]
fn the_kept_turns_start_at_an_assistant_message() {
    let messages = view(3);
    assert_eq!(keep_from(&messages, 1), Some(5));
    assert_eq!(keep_from(&messages, 2), Some(3));
    assert_eq!(keep_from(&messages, 3), None, "nothing older to fold");
    assert_eq!(keep_from(&messages, 10), None);
    assert_eq!(keep_from(&view(1), 1), None);
    assert_eq!(keep_from(&view(0), 1), None);
}

#[test]
fn a_second_compaction_needs_a_turn_besides_the_last_summary() {
    let mut messages = view(2);
    let summarized = replacement(&messages, summary_message("s"), 3);
    assert_eq!(keep_from(&summarized, 1), None, "only the summary is older");
    messages = summarized;
    messages.extend(tool_turn(9).into_iter().map(Arc::new));
    assert_eq!(keep_from(&messages, 1), Some(4));
}

#[test]
fn the_replacement_keeps_the_first_message_and_puts_the_summary_next() {
    let messages = view(3);
    let summary = summary_message("did things");
    let compacted = replacement(&messages, summary.clone(), 5);
    assert!(Arc::ptr_eq(&compacted[0], &messages[0]));
    assert_eq!(*compacted[1], summary);
    assert_eq!(compacted[1].role, Role::User);
    assert!(Arc::ptr_eq(&compacted[2], &messages[5]));
    assert_eq!(compacted.len(), 4);
    assert!(
        summary
            .text()
            .starts_with("<summary of earlier conversation>")
    );
}

#[test]
fn the_summary_request_disables_tools_and_keeps_the_conversation() {
    let mut spec = AgentSpec::new("m");
    spec.system_prompt = "be brief".into();
    let compaction = CompactionSpec {
        prompt: Some("recap".into()),
        ..CompactionSpec::new(1)
    };
    let tool = ToolDefinition {
        name: "Read".into(),
        description: String::new(),
        input_schema: Default::default(),
    };
    let messages: Vec<Message> = view(1).iter().map(|m| Message::clone(m)).collect();
    let request = summary_request(&spec, &compaction, vec![tool.clone()], messages.clone());
    assert_eq!(request.system[0].text, "recap");
    assert_eq!(request.tools, [tool]);
    assert_eq!(request.tool_choice, Some(ToolChoice::Disabled {}));
    assert_eq!(request.max_output_tokens, Some(4096));
    assert_eq!(request.messages, messages);
    let default = summary_request(&spec, &CompactionSpec::new(1), vec![], vec![]);
    assert_eq!(default.system[0].text, DEFAULT_PROMPT);
}
