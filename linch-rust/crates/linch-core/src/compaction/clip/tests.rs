use super::*;
use crate::message::{Role, ToolResultBlock, ToolUseBlock};
use serde_json::json;

fn call(id: &str) -> Message {
    let input = json!({"path": "a"}).as_object().unwrap().clone();
    let block = ToolUseBlock {
        id: id.into(),
        name: "Read".into(),
        input,
    };
    Message::new(Role::Assistant, vec![ContentBlock::ToolUse(block)])
}

fn result(id: &str, text: &str, is_error: bool) -> Message {
    let block = ToolResultBlock::text(id, text, is_error);
    Message::new(Role::User, vec![ContentBlock::ToolResult(block)])
}

/// The prompt, then one tool turn per entry of `sizes`: a result of that many
/// bytes.
fn view(sizes: &[usize]) -> Vec<Arc<Message>> {
    let mut messages = vec![Message::user_text("go")];
    for (i, size) in sizes.iter().enumerate() {
        messages.push(call(&format!("c{i}")));
        messages.push(result(&format!("c{i}"), &"x".repeat(*size), false));
    }
    messages.into_iter().map(Arc::new).collect()
}

fn text_of(message: &Message) -> String {
    match &message.content[0] {
        ContentBlock::ToolResult(result) => match &result.content[0] {
            ToolResultPart::Text(text) => text.text.clone(),
            ToolResultPart::Image(_) => String::new(),
        },
        ContentBlock::Text(text) => text.text.clone(),
        _ => String::new(),
    }
}

#[test]
fn a_view_under_the_budget_is_not_clipped() {
    let messages = view(&[400, 400]);
    let tokens = estimate_tokens(&messages);
    assert!(clip(&messages, tokens, Scope::Texts).is_none());
    assert!(clip(&messages, tokens + 50, Scope::Results).is_none());
}

#[test]
fn a_giant_result_keeps_its_head_and_tail_around_a_marker() {
    let mut messages = view(&[100]);
    let giant = format!(
        "{}{}{}",
        "H".repeat(3000),
        "m".repeat(20_000),
        "T".repeat(3000)
    );
    messages.extend([call("g"), result("g", &giant, false)].map(Arc::new));
    let budget = 1500;
    let clipped = clip(&messages, budget, Scope::Results).unwrap();
    let text = text_of(&clipped.messages[4]);
    assert!(text.starts_with('H') && text.ends_with('T'), "{text:.80}");
    assert!(text.contains("bytes omitted ...]"), "{text:.80}");
    assert!(estimate_tokens(&clipped.messages) <= budget);
    assert_eq!(
        clipped.bytes as usize,
        giant.len() - text.len(),
        "the bytes the cut took out, the marker counted"
    );
    // The small result and every message but the giant one are the originals.
    assert!(Arc::ptr_eq(&clipped.messages[2], &messages[2]));
    assert!(Arc::ptr_eq(&clipped.messages[3], &messages[3]));
    // The input is untouched.
    assert_eq!(text_of(&messages[4]), giant);
}

#[test]
fn many_medium_results_converge_under_one_cap() {
    let messages = view(&[6000; 12]);
    for budget in [9000, 4000, 2000, 1200] {
        let clipped = clip(&messages, budget, Scope::Results).unwrap();
        let tokens = estimate_tokens(&clipped.messages);
        assert!(tokens <= budget, "{tokens} > {budget}");
        // Every result was cut by the same cap: the same size.
        let sizes: Vec<usize> = (clipped.messages.iter().skip(2).step_by(2))
            .map(|m| text_of(m).len())
            .collect();
        assert!(sizes.windows(2).all(|w| w[0] == w[1]), "{sizes:?}");
    }
}

#[test]
fn a_small_result_is_cut_less_than_a_large_one_never_more() {
    let messages = view(&[300, 30_000, 3000]);
    let clipped = clip(&messages, 3000, Scope::Results).unwrap();
    let sizes: Vec<usize> = (clipped.messages.iter().skip(2).step_by(2))
        .map(|m| text_of(m).len())
        .collect();
    assert_eq!(
        (sizes[0], sizes[2]),
        (300, 3000),
        "under the cap, untouched"
    );
    assert!(sizes[1] < 30_000, "{sizes:?}");
}

#[test]
fn clipping_is_utf8_safe() {
    let word = "é".repeat(6000);
    let mut messages = view(&[]);
    messages.extend([call("u"), result("u", &word, false)].map(Arc::new));
    for budget in [3001, 2000, 777, 100] {
        let clipped = clip(&messages, budget, Scope::Results).unwrap();
        let text = text_of(&clipped.messages[2]);
        assert!(text.contains("bytes omitted"), "{budget}");
        assert!(text.starts_with('é') && text.ends_with('é'));
    }
}

#[test]
fn every_call_stays_paired_and_errors_stay_errors() {
    let mut messages = view(&[8000, 8000]);
    messages.extend([call("e"), result("e", &"y".repeat(8000), true)].map(Arc::new));
    let clipped = clip(&messages, 500, Scope::Texts).unwrap();
    assert_eq!(clipped.messages.len(), messages.len());
    for (new, old) in clipped.messages.iter().zip(&messages) {
        assert_eq!(new.role, old.role);
        for (a, b) in new.content.iter().zip(&old.content) {
            match (a, b) {
                (ContentBlock::ToolResult(a), ContentBlock::ToolResult(b)) => {
                    assert_eq!((&a.tool_use_id, a.is_error), (&b.tool_use_id, b.is_error));
                }
                (ContentBlock::ToolUse(a), ContentBlock::ToolUse(b)) => assert_eq!(a, b),
                _ => {}
            }
        }
    }
}

#[test]
fn tool_results_go_first_and_text_only_when_they_cannot_be_enough() {
    let long_text = "w".repeat(20_000);
    let mut messages = vec![Arc::new(Message::user_text(&long_text))];
    messages.extend([call("a"), result("a", &"x".repeat(4000), false)].map(Arc::new));
    // The result's cut is enough: the prompt stays whole.
    let enough = clip(&messages, 5300, Scope::Texts).unwrap();
    assert_eq!(text_of(&enough.messages[0]), long_text);
    // Not enough: the prompt is cut too, in `Texts` scope only.
    let both = clip(&messages, 300, Scope::Texts).unwrap();
    assert!(text_of(&both.messages[0]).contains("bytes omitted"));
    let results_only = clip(&messages, 300, Scope::Results).unwrap();
    assert_eq!(text_of(&results_only.messages[0]), long_text);
}

#[test]
fn nothing_long_enough_to_cut_is_none() {
    let messages = view(&[100, 100, 100]);
    assert!(clip(&messages, 1, Scope::Texts).is_none());
}

#[test]
fn a_view_clips_from_its_start_index_and_keeps_what_comes_before() {
    let messages = view(&[9000, 9000, 9000]);
    let clipped = clip_from(&messages, 5, 1000).unwrap();
    assert_eq!(clipped.messages.len(), messages.len());
    for (at, (new, old)) in clipped.messages.iter().zip(&messages).take(5).enumerate() {
        assert!(Arc::ptr_eq(new, old), "{at}");
    }
    assert!(text_of(&clipped.messages[6]).contains("bytes omitted"));
    assert!(clip_from(&messages, 99, 1).is_none());
}

#[test]
fn budgets_halve_with_each_refusal() {
    // The first delivery reads what the window leaves after the output.
    assert_eq!(folded_budget(8000, 4096, 0, 3300), 3904);
    // Then half of what the part weighs, or of that room if it is smaller.
    assert_eq!(folded_budget(8000, 4096, 1, 3300), 1650);
    assert_eq!(folded_budget(8000, 4096, 2, 3300), 825);
    assert_eq!(folded_budget(8000, 4096, 1, 90_000), 1952);
    assert_eq!(folded_budget(100, 4096, 3, 50), 1, "never zero");
    assert_eq!(last_turn_budget(8000, 0), Some(4000));
    assert_eq!(last_turn_budget(8000, 2), Some(1000));
    assert_eq!(last_turn_budget(8000, MAX_CLIP_ROUNDS + 1), None);
}
