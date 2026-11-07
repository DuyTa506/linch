//! One suite against two scripted targets.

use serde_json::json;

use super::*;
use crate::evals::{EvalCase, Scorer};
use crate::test_support::*;

fn capitals() -> EvalSuite {
    EvalSuite {
        name: "capitals".into(),
        cases: vec![
            EvalCase::new("Capital of France?", "Paris"),
            EvalCase::new("Capital of Germany?", "Berlin"),
        ],
        scorers: vec![
            Scorer::TextContains {
                substring: "{expected}".into(),
            },
            Scorer::RunCompleted {},
        ],
        metadata: json!({"owner": "evals"})
            .as_object()
            .cloned()
            .unwrap_or_default(),
    }
}

fn target(name: &str, answers: [&str; 2]) -> EvalBenchmarkTarget {
    let turns = answers.iter().map(|answer| text_turn(answer)).collect();
    EvalBenchmarkTarget::new(name, scripted(turns).agent)
}

async fn benchmark() -> EvalBenchmarkResult {
    let targets = [
        target("candidate-a", ["Paris", "Berlin"]),
        target("candidate-b", ["Paris", "Paris"]),
    ];
    run_eval_benchmark(&capitals(), &targets).await
}

#[tokio::test]
async fn each_target_runs_the_whole_suite() {
    let result = benchmark().await;
    assert_eq!(result.suite_name, "capitals");
    assert_eq!(
        result.scorer_names,
        ["text_contains:{expected}", "run_completed"]
    );
    assert_eq!(result.metadata["owner"], "evals");
    assert_eq!((result.total_targets(), result.passed_targets()), (2, 1));
    let passed: Vec<(&str, usize, usize)> = result
        .targets
        .iter()
        .map(|target| {
            let (total, passed) = (target.result.total(), target.result.passed());
            (target.name.as_str(), total, passed)
        })
        .collect();
    assert_eq!(passed, [("candidate-a", 2, 2), ("candidate-b", 2, 1)]);
    assert!(
        result
            .targets
            .iter()
            .all(|target| target.duration_ms >= 0.0)
    );
}

#[tokio::test]
async fn an_empty_suite_passes_every_target() {
    let suite = EvalSuite {
        cases: Vec::new(),
        ..capitals()
    };
    let targets = [target("idle", ["", ""])];
    let result = run_eval_benchmark(&suite, &targets).await;
    assert_eq!(result.passed_targets(), 1);
    assert_eq!(result.targets[0].result.pass_rate(), 0.0);
}

#[tokio::test]
async fn results_render_as_json_and_markdown() {
    let result = benchmark().await;
    let value = result.to_value(false);
    assert_eq!(value["suite"], "capitals");
    assert_eq!(
        value["scorers"],
        json!(["text_contains:{expected}", "run_completed"])
    );
    assert_eq!(value["metadata"], json!({"owner": "evals"}));
    assert_eq!(value["total_targets"], 2);
    assert_eq!(value["passed_targets"], 1);
    let second = &value["targets"][1];
    assert_eq!(second["name"], "candidate-b");
    assert_eq!(second["total"], 2);
    assert_eq!(second["passed"], 1);
    assert_eq!(second["pass_rate"], 0.5);
    assert!(second["duration_ms"].is_f64());
    assert_eq!(second["result"]["cases"][1]["output"], "Paris");

    let markdown = result.to_markdown();
    assert!(markdown.starts_with(
        "# Linch Eval Benchmark: capitals\n\n- targets: 2\n- passed_targets: 1\n\
         - scorers: text_contains:{expected}, run_completed\n\n\
         | Target | Cases | Passed | Pass Rate | Duration ms |\n"
    ));
    assert!(markdown.contains("\n| candidate-a | 2 | 2 | 100.00% | "));
    assert!(markdown.contains("\n| candidate-b | 2 | 1 | 50.00% | "));
    assert!(markdown.contains("\n## candidate-b\n\n# Linch Eval Report\n"));
}
