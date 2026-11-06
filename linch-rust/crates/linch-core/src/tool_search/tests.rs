use serde_json::json;

use super::Loads;
use super::tool::found;
use crate::message::{ContentBlock, Message, Role, ToolResultBlock, ToolUseBlock};

fn entries(pairs: &[(&'static str, &'static str)]) -> Vec<(&'static str, &'static str)> {
    pairs.to_vec()
}

fn names<'a>(found: &[&(&'a str, &'a str)]) -> Vec<&'a str> {
    found.iter().map(|(name, _)| *name).collect()
}

#[test]
fn select_takes_the_named_entries_in_their_order() {
    let all = entries(&[("b", ""), ("a", ""), ("c", "")]);
    let hits = found("select:c, a,zz", &all, |e| (e.0, e.1));
    assert_eq!(names(&hits), ["a", "c"]);
}

#[test]
fn keywords_rank_by_shared_words_then_name_and_keep_five() {
    let all = entries(&[
        ("zeta", "read files"),
        ("alpha", "read"),
        ("beta", "READ the files"),
        ("none", "nothing here"),
        ("c1", "read"),
        ("c2", "read"),
        ("c3", "read"),
    ]);
    let hits = found("Read, files!", &all, |e| (e.0, e.1));
    assert_eq!(names(&hits), ["beta", "zeta", "alpha", "c1", "c2"]);
    assert!(found("nomatch", &all, |e| (e.0, e.1)).is_empty());
}

#[test]
fn loads_are_read_from_successful_search_results() {
    let call = |id: &str, name: &str| ToolUseBlock {
        id: id.into(),
        name: name.into(),
        input: Default::default(),
    };
    let response = Message::new(
        Role::Assistant,
        vec![
            ContentBlock::ToolUse(call("s1", "tool_search")),
            ContentBlock::ToolUse(call("s2", "tool_search")),
            ContentBlock::ToolUse(call("e1", "Echo")),
        ],
    );
    let text = json!({"tools": [{"name": "Read"}], "skills": [{"name": "beta"}]}).to_string();
    let results = vec![
        ContentBlock::ToolResult(ToolResultBlock::text("s1", text.clone(), false)),
        ContentBlock::ToolResult(ToolResultBlock::text("s2", "not json", false)),
        ContentBlock::ToolResult(ToolResultBlock::text("e1", text, false)),
    ];
    let loads = Loads::of(&response, &results);
    assert_eq!(loads.tools.iter().collect::<Vec<_>>(), ["Read"]);
    assert_eq!(loads.skills.iter().collect::<Vec<_>>(), ["beta"]);
    let failed = [ContentBlock::ToolResult(ToolResultBlock::text(
        "s1", "{}", true,
    ))];
    assert!(Loads::of(&response, &failed).is_empty());
}
