//! Each built-in scorer on synthetic event streams, and the tool scorers
//! on a real scripted run.

use linch_core::events::kinds::{
    ContextBuildEvent, ResultEvent, ResultSubtype, ToolCallEndEvent, ToolCallStartEvent,
};
use linch_core::{Event, JsonObject, JsonValue, StopReason, ToolResultBlock, Usage};
use serde_json::json;

use super::*;
use crate::evals::run_eval;
use crate::test_support::*;

fn object(value: JsonValue) -> JsonObject {
    value.as_object().cloned().unwrap_or_default()
}

fn case() -> EvalCase {
    EvalCase::new("q", "")
}

fn start(id: &str, tool: &str) -> Event {
    Event::ToolCallStart(ToolCallStartEvent {
        tool_use_id: id.into(),
        tool_name: tool.into(),
        input: JsonObject::new(),
        summary: tool.into(),
    })
}

fn end(id: &str, tool: &str, is_error: bool) -> Event {
    Event::ToolCallEnd(ToolCallEndEvent {
        tool_name: tool.into(),
        result: ToolResultBlock::text(id, "out", is_error),
        duration_ms: 1,
        canonical_output: None,
    })
}

fn build(tools: &[&str], budget: JsonValue, metadata: JsonValue) -> Event {
    Event::ContextBuild(ContextBuildEvent {
        system_blocks: 1,
        messages: 1,
        selected_tools: Some(tools.iter().map(|tool| tool.to_string()).collect()),
        budget: object(budget),
        metadata: object(metadata),
    })
}

fn result(subtype: ResultSubtype) -> Event {
    Event::Result(ResultEvent {
        subtype,
        stop_reason: StopReason::EndTurn,
        total_usage: Usage::default(),
        usage_complete: true,
        final_text: None,
        structured_output: None,
        structured_error: None,
        trace_id: None,
    })
}

fn score(scorer: &Scorer, events: Vec<Event>) -> Option<bool> {
    scorer.score(&case(), "", &envelopes(events))
}

#[test]
fn tool_called_looks_for_a_started_call_of_the_tool() {
    let scorer = Scorer::ToolCalled {
        tool: "Read".into(),
    };
    assert_eq!(score(&scorer, vec![start("c1", "Read")]), Some(true));
    assert_eq!(score(&scorer, vec![start("c1", "Grep")]), Some(false));
    assert_eq!(score(&scorer, vec![]), Some(false));
}

#[test]
fn schema_valid_fails_closed() {
    let scorer = Scorer::SchemaValid {
        schema: object(json!({
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        })),
    };
    let check = |output: &str| scorer.score(&case(), output, &[]);
    assert_eq!(check(r#"{"answer": "Paris"}"#), Some(true));
    assert_eq!(check(r#"{"answer": 3}"#), Some(false));
    assert_eq!(check("Paris"), Some(false));
    let broken = Scorer::SchemaValid {
        schema: object(json!({"type": "no-such-type"})),
    };
    assert_eq!(broken.score(&case(), "{}", &[]), Some(false));
}

#[test]
fn context_scorers_are_unknown_without_a_context_build() {
    let scorers = [
        Scorer::ContextSelectedTool {
            tool: "Read".into(),
        },
        Scorer::ContextNotTrimmed {},
        Scorer::ContextMetadataContains {
            key: "rag".into(),
            expected: None,
        },
    ];
    for scorer in &scorers {
        assert_eq!(score(scorer, vec![start("c1", "Read")]), None, "{scorer:?}");
    }
}

#[test]
fn context_selected_tool_passes_when_any_build_offered_it() {
    let scorer = Scorer::ContextSelectedTool {
        tool: "Read".into(),
    };
    let none = build(&[], json!({}), json!({}));
    let read = build(&["Read"], json!({}), json!({}));
    assert_eq!(score(&scorer, vec![none.clone(), read]), Some(true));
    assert_eq!(score(&scorer, vec![none]), Some(false));
}

#[test]
fn context_not_trimmed_fails_on_any_trimmed_build() {
    let scorer = Scorer::ContextNotTrimmed {};
    let kept = build(&[], json!({"trimmed": false}), json!({}));
    let trimmed = build(&[], json!({"trimmed": true}), json!({}));
    assert_eq!(score(&scorer, vec![kept.clone()]), Some(true));
    assert_eq!(score(&scorer, vec![kept, trimmed]), Some(false));
}

#[test]
fn context_metadata_contains_matches_the_key_and_the_expected_value() {
    let any = Scorer::ContextMetadataContains {
        key: "rag".into(),
        expected: None,
    };
    let exact = Scorer::ContextMetadataContains {
        key: "rag".into(),
        expected: Some(json!("policy-1")),
    };
    let other = build(&[], json!({}), json!({"memory": 1}));
    let rag = build(&[], json!({}), json!({"rag": "policy-2"}));
    assert_eq!(score(&any, vec![other.clone(), rag.clone()]), Some(true));
    assert_eq!(score(&exact, vec![rag]), Some(false));
    let hit = build(&[], json!({}), json!({"rag": "policy-1"}));
    assert_eq!(score(&exact, vec![other.clone(), hit]), Some(true));
    assert_eq!(score(&any, vec![other]), Some(false));
}

/// Numbers compare by value, as Python's `==` does, at any depth.
#[test]
fn context_metadata_contains_compares_numbers_by_value() {
    let expect = |expected: JsonValue| Scorer::ContextMetadataContains {
        key: "k".into(),
        expected: Some(expected),
    };
    let built = |value: JsonValue| vec![build(&[], json!({}), json!({ "k": value }))];
    assert_eq!(score(&expect(json!(1)), built(json!(1.0))), Some(true));
    assert_eq!(score(&expect(json!(1.0)), built(json!(1))), Some(true));
    let nested = json!({"ids": [1, 2.5], "n": {"x": -3}});
    let same = json!({"n": {"x": -3.0}, "ids": [1.0, 2.5]});
    assert_eq!(score(&expect(nested.clone()), built(same)), Some(true));
    assert_eq!(score(&expect(json!(1)), built(json!(1.5))), Some(false));
    assert_eq!(
        score(&expect(json!([1])), built(json!([1, 1]))),
        Some(false)
    );
    assert_eq!(score(&expect(json!(1)), built(json!("1"))), Some(false));
    let large = json!(u64::MAX);
    assert_eq!(score(&expect(large.clone()), built(large)), Some(true));
    assert_eq!(
        score(&expect(json!(i64::MAX)), built(json!(u64::MAX))),
        Some(false)
    );
    assert_eq!(
        score(&expect(nested), built(json!({"ids": [1, 2.5]}))),
        Some(false)
    );
}

#[test]
fn recovery_needs_a_success_after_a_failure() {
    let any = Scorer::RecoverySucceeded { tool: None };
    let read = Scorer::RecoverySucceeded {
        tool: Some("Read".into()),
    };
    assert_eq!(score(&any, vec![]), None);
    assert_eq!(score(&any, vec![end("c1", "Read", false)]), Some(false));
    assert_eq!(score(&any, vec![end("c1", "Read", true)]), Some(false));
    let recovered = vec![end("c1", "Grep", true), end("c2", "Read", false)];
    assert_eq!(score(&any, recovered.clone()), Some(true));
    // Only calls of the named tool count.
    assert_eq!(score(&read, recovered), Some(false));
    assert_eq!(score(&read, vec![end("c1", "Grep", true)]), None);
}

#[test]
fn run_completed_reads_the_result_subtype() {
    let scorer = Scorer::RunCompleted {};
    assert_eq!(
        score(&scorer, vec![result(ResultSubtype::Success)]),
        Some(true)
    );
    assert_eq!(
        score(&scorer, vec![result(ResultSubtype::Error)]),
        Some(false)
    );
    assert_eq!(score(&scorer, vec![start("c1", "Read")]), None);
}

#[test]
fn names_follow_the_python_scorers() {
    let names: Vec<String> = [
        Scorer::ToolCalled {
            tool: "Read".into(),
        },
        Scorer::SchemaValid {
            schema: JsonObject::new(),
        },
        Scorer::ContextSelectedTool {
            tool: "Read".into(),
        },
        Scorer::ContextNotTrimmed {},
        Scorer::ContextMetadataContains {
            key: "rag".into(),
            expected: None,
        },
        Scorer::RecoverySucceeded { tool: None },
        Scorer::RecoverySucceeded {
            tool: Some("Read".into()),
        },
        Scorer::RunCompleted {},
    ]
    .iter()
    .map(Scorer::name)
    .collect();
    assert_eq!(
        names,
        [
            "tool_called:Read",
            "schema_valid",
            "context_selected_tool:Read",
            "context_not_trimmed",
            "context_metadata_contains:rag",
            "recovery_succeeded:*",
            "recovery_succeeded:Read",
            "run_completed",
        ]
    );
}

#[tokio::test]
async fn tool_scorers_read_a_real_run_and_keep_duplicate_names() {
    let fixture = scripted(vec![
        tool_turn("c1", "Probe", json!({"fail": true})),
        tool_turn("c2", "Probe", json!({})),
        text_turn("{}"),
    ]);
    let empty = Scorer::SchemaValid {
        schema: object(json!({"type": "object"})),
    };
    let strict = Scorer::SchemaValid {
        schema: object(json!({"type": "object", "required": ["answer"]})),
    };
    let scorers = [
        Scorer::ToolCalled {
            tool: "Probe".into(),
        },
        Scorer::RecoverySucceeded {
            tool: Some("Probe".into()),
        },
        empty,
        strict,
    ];
    let result = run_eval(&fixture.agent, &[case()], &scorers).await;

    let case = &result.cases[0];
    let verdicts: Vec<(&str, Option<bool>)> = case
        .scores
        .iter()
        .map(|score| (score.name.as_str(), score.verdict))
        .collect();
    assert_eq!(
        verdicts,
        [
            ("tool_called:Probe", Some(true)),
            ("recovery_succeeded:Probe", Some(true)),
            ("schema_valid", Some(true)),
            ("schema_valid", Some(false)),
        ]
    );
    assert!(!case.passed);
    assert_eq!(case.error, None);
}
