use serde_json::json;

use super::*;
use crate::compaction::estimate_tokens;
use crate::message::Role;
use std::sync::Arc;

fn results(texts: &[(&str, String)]) -> Message {
    let blocks = (texts.iter())
        .map(|(id, text)| ContentBlock::ToolResult(ToolResultBlock::text(*id, text, false)))
        .collect();
    Message::new(Role::User, blocks)
}

fn text_of(message: &Message, at: usize) -> &str {
    match &message.content[at] {
        ContentBlock::ToolResult(result) => match &result.content[..] {
            [ToolResultPart::Text(text)] => &text.text,
            other => panic!("{other:?}"),
        },
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_result_at_or_under_the_cap_is_not_touched() {
    let cap = ToolResultCap::new(200);
    for len in [0, 1, 799, 800] {
        let message = results(&[("t1", "x".repeat(len))]);
        assert_eq!(cap.apply(&message), None, "{len}");
    }
    assert!(cap.apply(&results(&[("t1", "x".repeat(801))])).is_some());
}

#[test]
fn a_cut_result_keeps_its_head_and_tail_and_fits_the_budget() {
    let cap = ToolResultCap::new(200);
    let text: String = (0..5000)
        .map(|i| char::from(b'a' + (i % 26) as u8))
        .collect();
    let message = results(&[("toolu_1", text.clone())]);
    let capped = cap.apply(&message).unwrap();
    let shown = text_of(&capped, 0);
    assert!(shown.len() <= cap.budget(), "{}", shown.len());
    assert!(estimate_tokens(&[Arc::new(capped.clone())]) <= 200);
    // Head about 30% of what is kept, tail the rest.
    let (head, rest) = shown.split_once("\n[... ").unwrap();
    let (marker, tail) = rest.split_once(" ...]\n").unwrap();
    assert!(text.starts_with(head) && text.ends_with(tail));
    assert!(head.len() < tail.len(), "{} {}", head.len(), tail.len());
    let omitted = text.len() - head.len() - tail.len();
    let want = format!(
        "{omitted} bytes omitted; read them with ReadToolResult(tool_use_id=\"toolu_1\", offset={})",
        head.len()
    );
    assert_eq!(marker, want);
}

#[test]
fn the_cut_is_the_same_every_time() {
    let cap = ToolResultCap::new(300);
    let message = results(&[("a", "y".repeat(20_000)), ("b", "z".repeat(10))]);
    let one = cap.apply(&message).unwrap();
    assert_eq!(cap.apply(&message), Some(one.clone()));
    // The under-cap result of the same message is the original.
    assert_eq!(text_of(&one, 1), "z".repeat(10));
}

#[test]
fn a_cut_never_splits_a_character() {
    let cap = ToolResultCap::new(128);
    for text in ["é".repeat(3000), "漢".repeat(3000), "🦀a".repeat(2000)] {
        let capped = cap.apply(&results(&[("t", text)])).unwrap();
        // `text_of` returned a `&str`: the slices were on boundaries.
        assert!(text_of(&capped, 0).len() <= cap.budget());
    }
}

#[test]
fn the_head_fraction_sets_the_share_of_the_head() {
    let text = "q".repeat(10_000);
    let head_of = |fraction: f64| {
        let cap = ToolResultCap {
            head_fraction: fraction,
            ..ToolResultCap::new(500)
        };
        let capped = cap.apply(&results(&[("t", text.clone())])).unwrap();
        let shown = text_of(&capped, 0).to_owned();
        shown.split_once("\n[... ").unwrap().0.len()
    };
    assert_eq!(head_of(0.0), 0);
    assert!(head_of(0.5) > head_of(0.3));
    assert!(head_of(1.0) > head_of(0.5));
}

#[test]
fn images_stay_and_several_text_parts_become_one() {
    let image = crate::message::ImageBlock {
        source: crate::message::ImageSource::Base64 {
            media_type: "image/png".into(),
            data: "AAAA".into(),
        },
    };
    let result = ToolResultBlock {
        tool_use_id: "t".into(),
        content: vec![
            ToolResultPart::Text(TextBlock {
                text: "a".repeat(600),
            }),
            ToolResultPart::Image(image.clone()),
            ToolResultPart::Text(TextBlock {
                text: "b".repeat(600),
            }),
        ],
        is_error: true,
    };
    let message = Message::new(Role::User, vec![ContentBlock::ToolResult(result)]);
    let capped = ToolResultCap::new(128).apply(&message).unwrap();
    let ContentBlock::ToolResult(result) = &capped.content[0] else {
        panic!()
    };
    assert!(result.is_error);
    assert!(
        matches!(&result.content[..], [ToolResultPart::Text(_), ToolResultPart::Image(i)] if *i == image)
    );
}

#[test]
fn a_cap_must_leave_room_for_its_marker() {
    assert!(ToolResultCap::new(127).problem().is_some());
    assert!(ToolResultCap::new(128).problem().is_none());
    for fraction in [-0.1, 1.1, f64::NAN] {
        let cap = ToolResultCap {
            head_fraction: fraction,
            ..ToolResultCap::new(500)
        };
        assert!(cap.problem().is_some(), "{fraction}");
    }
}

#[test]
fn the_spec_reads_with_its_default_head() {
    let cap: ToolResultCap = serde_json::from_value(json!({"max_tokens": 8000})).unwrap();
    assert_eq!(cap, ToolResultCap::new(8000));
    assert!(serde_json::from_value::<ToolResultCap>(json!({"max_tokens": 8000, "x": 1})).is_err());
    assert!(serde_json::from_value::<ToolResultCap>(json!({})).is_err());
}
