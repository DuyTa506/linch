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
    // A forced choice of the agent never reaches the summary call.
    spec.tool_choice = Some(ToolChoice::Required {});
    let forced = summary_request(&spec, &compaction, vec![], vec![]);
    assert_eq!(forced.tool_choice, Some(ToolChoice::Disabled {}));
}

#[test]
fn the_summary_request_is_not_held_to_the_output_schema() {
    let mut spec = AgentSpec::new("m");
    let schema = serde_json::json!({"name": "answer", "schema": {"type": "object"}});
    spec.output_schema = Some(serde_json::from_value(schema).unwrap());
    let request = summary_request(&spec, &CompactionSpec::new(1), vec![], vec![]);
    assert_eq!(request.output_schema, None, "a summary is free text");
}

#[test]
fn without_a_threshold_it_is_due_at_80_percent_of_the_window_less_the_reserve() {
    let by_window: CompactionSpec = serde_json::from_value(json!({})).unwrap();
    assert_eq!(by_window, CompactionSpec::default());
    assert_eq!(by_window.threshold_tokens, None);
    let wire = serde_json::to_value(&by_window).unwrap();
    assert!(wire.get("threshold_tokens").is_none());
    // Python's reserve: a fifth of the window, at least 1, at most 32768.
    assert!(!due(&by_window, None, 100_000, 59_999));
    assert!(due(&by_window, None, 100_000, 60_000));
    assert!(!due(&by_window, None, 1_000_000, 800_000 - 32_768 - 1));
    assert!(due(&by_window, None, 1_000_000, 800_000 - 32_768));
    assert!(!due(&by_window, None, 4, 2));
    assert!(due(&by_window, None, 4, 3));
    // The spec's output limit is the reserve when set, unclamped.
    assert!(due(&by_window, Some(50_000), 100_000, 30_000));
    assert!(!due(&by_window, Some(50_000), 100_000, 29_999));
    assert!(
        due(&by_window, Some(0), 100_000, 60_000),
        "0 counts as unset"
    );
    // An explicit threshold wins over the window.
    let explicit = CompactionSpec::new(10);
    assert!(due(&explicit, None, 1_000_000, 10));
    assert!(!due(&explicit, Some(1), 1, 9));
}

#[test]
fn the_ladder_fields_stay_off_the_wire_at_their_defaults() {
    let spec = CompactionSpec::new(9);
    assert!(!spec.micro);
    assert_eq!(spec.max_forced, 3);
    assert_eq!(
        serde_json::to_value(&spec).unwrap(),
        json!({"threshold_tokens": 9, "keep_recent_turns": 10, "max_output_tokens": 4096}),
        "a spec written before the ladder keeps its contract"
    );
    let set: CompactionSpec =
        serde_json::from_value(json!({"micro": true, "max_forced": 0})).unwrap();
    assert!(set.micro);
    assert_eq!(set.max_forced, 0);
    assert_eq!(
        serde_json::to_value(&set).unwrap()["micro"],
        json!(true),
        "a set field is part of the contract"
    );
}

/// The prompt, then `n` tool turns whose results are `size` bytes long.
fn long_view(n: usize, size: usize) -> Vec<Arc<Message>> {
    let mut messages = vec![Arc::new(Message::user_text("go"))];
    for i in 0..n {
        let [call, _] = tool_turn(i);
        let result = ToolResultBlock::text(format!("c{i}"), "x".repeat(size), false);
        messages.push(Arc::new(call));
        messages.push(Arc::new(Message::new(
            Role::User,
            vec![ContentBlock::ToolResult(result)],
        )));
    }
    messages
}

fn result_text(message: &Message) -> String {
    match &message.content[0] {
        ContentBlock::ToolResult(result) => match &result.content[0] {
            ToolResultPart::Text(text) => text.text.clone(),
            ToolResultPart::Image(_) => "image".into(),
        },
        _ => panic!("not a tool result"),
    }
}

#[test]
fn micro_compaction_clears_old_results_and_keeps_every_call_paired() {
    let view = long_view(3, 100);
    let (before, cleared) = clear_tool_results(&view, 1).unwrap();
    assert_eq!(before, 5, "the last assistant turn is kept");
    assert_eq!(cleared.len(), view.len());
    assert_eq!(result_text(&cleared[2]), CLEARED_RESULT);
    assert_eq!(result_text(&cleared[4]), CLEARED_RESULT);
    assert_eq!(result_text(&cleared[6]), "x".repeat(100), "kept as it was");
    for at in [0, 1, 3, 5, 6] {
        assert!(Arc::ptr_eq(&view[at], &cleared[at]), "{at} is shared");
    }
    match &cleared[2].content[0] {
        ContentBlock::ToolResult(result) => assert_eq!(result.tool_use_id, "c0"),
        _ => panic!("not a tool result"),
    }
    assert!(estimate_tokens(&cleared) < estimate_tokens(&view));
    assert_eq!(
        clear_tool_results(&cleared, 1),
        None,
        "nothing left to clear"
    );
    assert_eq!(cleared_before(&view, before), Some(cleared));
}

#[test]
fn micro_compaction_skips_short_results_and_short_views() {
    // "text" is shorter than the placeholder.
    assert_eq!(clear_tool_results(&view(3), 1), None);
    assert_eq!(clear_tool_results(&long_view(1, 100), 1), None);
    assert_eq!(clear_tool_results(&long_view(3, 100), 10), None);
    let image = ToolResultBlock {
        tool_use_id: "c0".into(),
        content: vec![ToolResultPart::Image(ImageBlock {
            source: ImageSource::Url { url: "u".into() },
        })],
        is_error: false,
    };
    let mut view = long_view(2, 1);
    view[2] = Arc::new(Message::new(
        Role::User,
        vec![ContentBlock::ToolResult(image)],
    ));
    let (_, cleared) = clear_tool_results(&view, 1).unwrap();
    assert_eq!(
        result_text(&cleared[2]),
        CLEARED_RESULT,
        "an image is cleared"
    );
}

#[test]
fn a_forced_summary_keeps_only_the_last_turn_when_the_kept_turns_fold_nothing() {
    let view = long_view(3, 10);
    assert_eq!(forced_keep_from(&view, 2), Some(3));
    assert_eq!(forced_keep_from(&view, 10), Some(5));
    assert_eq!(forced_keep_from(&long_view(1, 10), 10), None);
}

mod forced {
    use super::super::Forced;
    use crate::events::model_call::{ModelCallDeliveryEvent, ModelCallKind};
    use crate::events::{Envelope, Event};
    use crate::ids::RunId;

    fn delivery(seq: u64, kind: ModelCallKind, turn: u64) -> Envelope {
        let event = Event::ModelCallDelivery(ModelCallDeliveryEvent {
            run_id: RunId::new("r"),
            logical_call_id: format!("r:{kind:?}:{turn}"),
            delivery_attempt_id: format!("r:{kind:?}:{turn}:{seq}"),
            kind,
            turn_index: Some(turn),
            requested_model: "m".into(),
        });
        Envelope {
            v: crate::events::WIRE_VERSION,
            seq,
            run_id: RunId::new("r"),
            ts_ms: 0,
            ignorable: false,
            event,
        }
    }

    fn forced(spent: u32, summary_calls: u32, primary_calls: u32, latest: bool) -> Forced {
        Forced {
            spent,
            summary_calls,
            primary_calls,
            latest_forced: latest,
        }
    }

    #[test]
    fn a_summary_after_a_primary_call_of_its_turn_is_forced() {
        use ModelCallKind::{Compaction as C, Primary as P};
        let events: Vec<_> = [(C, 0), (P, 0), (P, 1), (C, 1), (C, 1), (P, 1), (C, 2)]
            .into_iter()
            .enumerate()
            .map(|(seq, (kind, turn))| delivery(seq as u64, kind, turn))
            .collect();
        let at = |turn, upto: usize| Forced::of(&events[..upto], turn);
        assert_eq!(at(0, 2), forced(0, 1, 1, false));
        assert_eq!(at(1, 4), forced(1, 1, 1, true));
        assert_eq!(
            at(1, 5),
            forced(1, 2, 1, true),
            "a second attempt of one summary is not another"
        );
        assert_eq!(at(1, 6), forced(1, 2, 2, true));
        assert_eq!(at(2, 7), forced(1, 1, 0, false));
    }
}
