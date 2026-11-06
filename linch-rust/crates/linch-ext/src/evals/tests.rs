//! `run_eval` end to end on the scripted provider and memory stores.

use std::sync::atomic::Ordering;

use linch_core::Event;
use serde_json::json;

use super::*;
use crate::test_support::*;

fn expected() -> Scorer {
    Scorer::TextContains {
        substring: "{expected}".into(),
    }
}

#[tokio::test]
async fn a_case_passes_when_its_output_contains_the_expected_answer() {
    let fixture = scripted(vec![text_turn("It is PARIS."), text_turn("Munich")]);
    let cases = [
        EvalCase::new("Capital of France?", "Paris"),
        EvalCase::new("Capital of Germany?", "Berlin"),
    ];
    let result = run_eval(&fixture.agent, &cases, &[expected()]).await;

    assert_eq!((result.total(), result.passed()), (2, 1));
    assert_eq!(result.pass_rate(), 0.5);
    let (first, second) = (&result.cases[0], &result.cases[1]);
    assert_eq!(first.output, "It is PARIS.");
    assert!(first.passed);
    assert_eq!(
        first.scores,
        [Score {
            name: "text_contains:{expected}".into(),
            verdict: Some(true),
        }]
    );
    assert!(!second.passed);
    assert_eq!(second.scores[0].verdict, Some(false));
    assert_eq!(second.error, None);
    assert!(matches!(
        second.events.last().map(|envelope| &envelope.event),
        Some(Event::Result(_))
    ));
}

#[tokio::test]
async fn every_case_runs_on_a_fresh_session_that_is_closed_after() {
    let fixture = scripted(vec![text_turn("a"), text_turn("b"), text_turn("c")]);
    let cases = [EvalCase::new("one", ""), EvalCase::new("two", "")];
    run_eval(&fixture.agent, &cases, &[]).await;
    // A second eval on the same agent must not see the first one's turns.
    run_eval(&fixture.agent, &cases[..1], &[]).await;

    let requests = fixture.provider.requests();
    assert_eq!(requests.len(), 3);
    assert!(requests.iter().all(|request| request.messages.len() == 1));
    assert_eq!(fixture.closed.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn a_run_that_fails_records_its_error_and_fails_the_case() {
    // No turn is scripted: the provider call fails and the run ends failed.
    let fixture = scripted(vec![]);
    let result = run_eval(&fixture.agent, &[EvalCase::new("hi", "")], &[]).await;

    let case = &result.cases[0];
    assert!(!case.passed);
    assert_eq!(case.output, "");
    let error = case.error.as_deref().unwrap_or_default();
    assert!(error.contains("ran out of turns"), "{error}");
}

#[tokio::test]
async fn an_unknown_verdict_does_not_fail_the_case() {
    // A write tool asks for approval, so the run suspends without a result.
    let fixture = scripted(vec![tool_turn("c1", "Edit", json!({}))]);
    let scorers = [Scorer::RunCompleted {}];
    let result = run_eval(&fixture.agent, &[EvalCase::new("edit", "")], &scorers).await;

    let case = &result.cases[0];
    assert_eq!(case.scores[0].verdict, None);
    assert!(case.passed);
    assert!(matches!(
        case.events.last().map(|envelope| &envelope.event),
        Some(Event::Suspended(_))
    ));
}

#[tokio::test]
async fn run_completed_passes_a_successful_run() {
    let fixture = scripted(vec![text_turn("done")]);
    let scorers = [Scorer::RunCompleted {}];
    let result = run_eval(&fixture.agent, &[EvalCase::new("go", "")], &scorers).await;
    assert_eq!(result.cases[0].scores[0].verdict, Some(true));
}

#[test]
fn text_contains_ignores_case_and_fills_in_every_expected() {
    let case = EvalCase::new("q", "Paris");
    let scorer = Scorer::TextContains {
        substring: "{expected}, {expected}".into(),
    };
    assert_eq!(scorer.score(&case, "paris, PARIS!", &[]), Some(true));
    assert_eq!(scorer.score(&case, "paris", &[]), Some(false));
    assert_eq!(scorer.name(), "text_contains:{expected}, {expected}");
}

#[tokio::test]
async fn results_render_as_json_and_markdown() {
    let fixture = scripted(vec![
        tool_turn("c1", "Probe", json!({})),
        text_turn("a | b\nc"),
    ]);
    let result = run_eval(&fixture.agent, &[EvalCase::new("q", "a")], &[expected()]).await;
    let case = &result.cases[0];
    assert_eq!(case.tool_calls(), ["Probe"]);

    let value = result.to_value(false);
    assert_eq!(value["total"], 1);
    assert_eq!(value["pass_rate"], 1.0);
    let case_value = &value["cases"][0];
    assert_eq!(case_value["case"]["prompt"], "q");
    assert_eq!(case_value["tool_calls"], json!(["Probe"]));
    assert_eq!(case_value["event_count"], case.events.len());
    assert_eq!(
        case_value["scores"],
        json!([{"name": "text_contains:{expected}", "verdict": true}])
    );
    assert!(case_value.get("events").is_none());
    let with_events = result.to_value(true);
    let events = with_events["cases"][0]["events"].as_array().unwrap();
    assert_eq!(events.len(), case.events.len());
    assert_eq!(events[0]["event"]["type"], "init");

    let markdown = result.to_markdown();
    assert!(markdown.starts_with("# Linch Eval Report\n\n- total: 1\n- passed: 1\n"));
    assert!(markdown.contains("- pass_rate: 100.00%"));
    assert!(
        markdown.contains("| 1 | true | text_contains:{expected}=true | Probe | a \\| b c |  |")
    );
}

#[test]
fn a_long_cell_is_cut_to_120_characters_on_a_char_boundary() {
    let long = "é".repeat(130);
    let text = super::harness::cell(&long);
    assert_eq!(text.chars().count(), 120);
    assert!(text.ends_with("..."));
}
