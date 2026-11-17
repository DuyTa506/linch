use serde_json::json;

use super::*;

/// A block of `tokens` estimated tokens, named by `label`.
fn block(source: &str, index: usize, label: char, tokens: usize) -> Sourced {
    Sourced {
        source: Arc::from(source),
        index,
        block: TextBlock {
            text: label.to_string().repeat(tokens * 4),
        },
    }
}

fn labels(trimmed: &Trimmed) -> String {
    trimmed
        .blocks
        .iter()
        .map(|b| b.text.chars().next().unwrap())
        .collect()
}

#[test]
fn a_block_costs_four_characters_a_token_rounded_up() {
    assert_eq!(tokens(""), 0);
    assert_eq!(tokens("abcd"), 1);
    assert_eq!(tokens("abcde"), 2);
}

#[test]
fn context_that_fits_is_kept_whole_and_reported() {
    let budget = ContextBudget::new(20);
    let trimmed = budget.apply(vec![block("a", 0, 'x', 5), block("b", 0, 'y', 5)]);
    assert_eq!(labels(&trimmed), "xy");
    assert_eq!(
        serde_json::to_value(&trimmed.report).unwrap(),
        json!({
            "max_tokens": 20, "used_tokens": 10, "remaining_tokens": 10,
            "trimmed": false, "dropped": []
        })
    );
}

#[test]
fn unlisted_hooks_go_first_the_later_one_first() {
    let budget = ContextBudget::new(10);
    let blocks = vec![
        block("early", 0, 'a', 5),
        block("late", 0, 'b', 5),
        block("last", 0, 'c', 5),
    ];
    let trimmed = budget.apply(blocks);
    assert_eq!(labels(&trimmed), "ab", "the last hook's block went");
    assert_eq!(trimmed.report["dropped"][0]["source"], "last");
    assert_eq!(trimmed.report["trimmed"], true);
}

#[test]
fn the_declared_order_decides_among_listed_hooks_and_pinned_stay() {
    let mut budget = ContextBudget::new(12);
    budget.drop_order = vec!["memory".into(), "files".into()];
    budget.pinned = vec!["policy".into()];
    let blocks = vec![
        block("policy", 0, 'p', 5),
        block("files", 0, 'f', 5),
        block("memory", 0, 'm', 5),
        block("other", 0, 'o', 5),
    ];
    let trimmed = budget.apply(blocks);
    // 20 tokens, 12 allowed: the unlisted `other`, then `memory` go.
    assert_eq!(labels(&trimmed), "pf");
    let gone: Vec<_> = trimmed.report["dropped"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["source"].as_str().unwrap())
        .collect();
    assert_eq!(gone, ["memory", "other"]);
    assert_eq!(trimmed.report["used_tokens"], 10);
}

#[test]
fn inside_a_hook_the_last_block_goes_first_and_the_index_names_it() {
    let budget = ContextBudget::new(10);
    let blocks = (0..4).map(|i| block("memory", i, 'a', 4)).collect();
    let trimmed = budget.apply(blocks);
    assert_eq!(trimmed.blocks.len(), 2);
    let indexes: Vec<_> = trimmed.report["dropped"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["index"].as_u64().unwrap())
        .collect();
    assert_eq!(indexes, [2, 3]);
}

#[test]
fn nothing_more_is_dropped_than_the_cap_needs() {
    let budget = ContextBudget::new(9);
    let trimmed = budget.apply(vec![block("a", 0, 'x', 1), block("b", 0, 'y', 10)]);
    assert_eq!(labels(&trimmed), "x");
    let budget = ContextBudget::new(11);
    let trimmed = budget.apply(vec![block("a", 0, 'x', 1), block("b", 0, 'y', 10)]);
    assert_eq!(labels(&trimmed), "xy");
}

#[test]
fn pinned_blocks_over_the_cap_stay_and_the_report_says_by_how_much() {
    let mut budget = ContextBudget::new(4);
    budget.pinned = vec!["policy".into()];
    let trimmed = budget.apply(vec![block("policy", 0, 'p', 10), block("x", 0, 'x', 1)]);
    assert_eq!(labels(&trimmed), "p");
    assert_eq!(trimmed.report["over_by"], 6);
    assert_eq!(trimmed.report["remaining_tokens"], 0);
}

#[test]
fn a_budget_that_cannot_apply_is_named() {
    assert_eq!(ContextBudget::new(1).problem(), None);
    assert!(ContextBudget::new(0).problem().is_some());
    let mut twice = ContextBudget::new(1);
    twice.drop_order = vec!["a".into()];
    twice.pinned = vec!["a".into()];
    assert!(twice.problem().unwrap().contains("twice"));
    let mut blank = ContextBudget::new(1);
    blank.pinned = vec![" ".into()];
    assert!(blank.problem().is_some());
}

#[test]
fn the_wire_form_leaves_empty_lists_out_and_refuses_unknown_keys() {
    let budget = ContextBudget::new(100);
    let wire = serde_json::to_value(&budget).unwrap();
    assert_eq!(wire, json!({"max_tokens": 100}));
    assert_eq!(
        serde_json::from_value::<ContextBudget>(wire).unwrap(),
        budget
    );
    let wrong = json!({"max_tokens": 1, "priority": {}});
    assert!(serde_json::from_value::<ContextBudget>(wrong).is_err());
}
