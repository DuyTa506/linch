//! The suite file schema.

use serde_json::json;

use super::*;

/// The suite from `docs/usage/evals.md`.
const CAPITALS: &str = r#"{
  "name": "capitals",
  "cases": [
    {"prompt": "Capital of France?", "expected": "Paris"},
    {"prompt": "Capital of Germany?", "expected": "Berlin"}
  ],
  "scorers": [
    {"type": "text_contains", "substring": "{expected}"},
    {"type": "run_completed"}
  ]
}"#;

fn decode_error(text: &str) -> String {
    EvalSuite::from_json(text).unwrap_err().to_string()
}

#[test]
fn the_documented_suite_decodes() {
    let suite = EvalSuite::from_json(CAPITALS).unwrap();
    assert_eq!(suite.name, "capitals");
    assert_eq!(
        suite.cases,
        [
            EvalCase::new("Capital of France?", "Paris"),
            EvalCase::new("Capital of Germany?", "Berlin"),
        ]
    );
    assert_eq!(
        suite.scorer_names(),
        ["text_contains:{expected}", "run_completed"]
    );
    assert!(suite.metadata.is_empty());
}

#[test]
fn every_scorer_type_decodes_with_its_fields() {
    let text = json!({
        "cases": [{"prompt": "q", "metadata": {"area": "memory"}}],
        "scorers": [
            {"type": "text_contains"},
            {"type": "tool_called", "tool": "Read"},
            {"type": "schema_valid", "schema": {"type": "object"}},
            {"type": "context_selected_tool", "tool": "Read"},
            {"type": "context_not_trimmed"},
            {"type": "context_metadata_contains", "key": "rag", "expected": "p1"},
            {"type": "context_metadata_contains", "key": "rag", "expected": null},
            {"type": "recovery_succeeded"},
            {"type": "recovery_succeeded", "tool": "Read"},
            {"type": "run_completed"}
        ],
        "metadata": {"owner": "evals"}
    })
    .to_string();
    let suite = EvalSuite::from_json(&text).unwrap();
    assert_eq!(suite.cases[0].expected, "");
    assert_eq!(suite.cases[0].metadata["area"], "memory");
    assert_eq!(suite.metadata["owner"], "evals");
    assert_eq!(
        suite.scorers[0],
        Scorer::TextContains {
            substring: "{expected}".into()
        }
    );
    assert_eq!(
        suite.scorers[5],
        Scorer::ContextMetadataContains {
            key: "rag".into(),
            expected: Some(json!("p1")),
        }
    );
    assert_eq!(
        suite.scorers[6],
        Scorer::ContextMetadataContains {
            key: "rag".into(),
            expected: None,
        }
    );
    assert_eq!(suite.scorers[7], Scorer::RecoverySucceeded { tool: None });
    // The decoded suite encodes back to a suite that decodes the same.
    let again = serde_json::to_string(&suite).unwrap();
    assert_eq!(EvalSuite::from_json(&again).unwrap(), suite);
}

#[test]
fn unknown_types_and_fields_are_rejected() {
    let unknown = r#"{"cases": [], "scorers": [{"type": "cost_under", "budget_usd": 1}]}"#;
    assert!(decode_error(unknown).contains("cost_under"));
    // Python's `text` and `contains` aliases are not accepted.
    let alias = r#"{"cases": [], "scorers": [{"type": "text_contains", "text": "x"}]}"#;
    assert!(decode_error(alias).contains("text"));
    let extra = r#"{"cases": [], "scorers": [{"type": "run_completed", "x": 1}]}"#;
    assert!(decode_error(extra).contains("x"));
    let case = r#"{"cases": [{"prompt": "q", "answer": "a"}]}"#;
    assert!(decode_error(case).contains("answer"));
    assert!(decode_error(r#"{"name": "no cases"}"#).contains("cases"));
}

#[test]
fn empty_strings_are_rejected_with_their_path() {
    let cases = [
        (
            r#"{"cases": [{"prompt": "q"}, {"prompt": ""}]}"#,
            "cases[1].prompt",
        ),
        (
            r#"{"cases": [], "scorers": [{"type": "text_contains", "substring": ""}]}"#,
            "scorers[0].substring",
        ),
        (
            r#"{"cases": [], "scorers": [{"type": "run_completed"}, {"type": "tool_called", "tool": ""}]}"#,
            "scorers[1].tool",
        ),
        (
            r#"{"cases": [], "scorers": [{"type": "recovery_succeeded", "tool": ""}]}"#,
            "scorers[0].tool",
        ),
        (
            r#"{"cases": [], "scorers": [{"type": "context_metadata_contains", "key": ""}]}"#,
            "scorers[0].key",
        ),
    ];
    for (text, path) in cases {
        assert_eq!(
            decode_error(text),
            format!("invalid suite: {path} must be a non-empty string")
        );
    }
}

#[test]
fn a_file_without_a_name_is_named_after_its_stem() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("smoke.json");
    std::fs::write(&path, r#"{"cases": [{"prompt": "q"}]}"#).unwrap();
    assert_eq!(load_eval_suite(&path).unwrap().name, "smoke");

    std::fs::write(&path, CAPITALS).unwrap();
    assert_eq!(load_eval_suite(&path).unwrap().name, "capitals");

    let missing = dir.path().join("missing.json");
    let error = load_eval_suite(&missing).unwrap_err();
    assert!(matches!(error, SuiteError::Read { .. }), "{error}");
}
