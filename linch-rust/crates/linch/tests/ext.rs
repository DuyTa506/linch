//! The extensions are reachable through the facade as `linch::ext`.

use linch::AgentConfig;
use linch::ext::evals::{EvalCase, Scorer, run_eval};
use linch::provider::scripted::TurnBuilder;
use serde_json::json;

#[tokio::test]
async fn an_agent_opened_from_a_config_runs_an_eval_through_ext() {
    let turns = json!([TurnBuilder::new("scripted-model").text("Paris").build()]);
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": turns},
    }))
    .unwrap();
    let opened = config.open().await.unwrap();
    let cases = [EvalCase::new("Capital of France?", "Paris")];
    let scorers = [Scorer::RunCompleted {}];
    let result = run_eval(opened.agent(), &cases, &scorers).await;
    assert_eq!((result.total(), result.passed()), (1, 1));
}
