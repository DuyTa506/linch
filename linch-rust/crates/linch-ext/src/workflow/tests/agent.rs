//! An agent run journaled as a step, on the scripted provider.

use std::convert::Infallible;
use std::sync::Arc;

use linch_core::{Event, RunOptions};

use super::*;
use crate::test_support::{drain, scripted, text_turn};

#[tokio::test]
async fn an_agent_run_inside_a_step_replays_without_a_provider_call() {
    let fixture = scripted(vec![text_turn("LGTM")]);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move {
            let review =
                wf.step("review", || {
                    let agent = agent.clone();
                    async move {
                        let run = agent.run("review the diff", RunOptions::new("review-1"));
                        let final_text = drain(run).await.into_iter().find_map(|envelope| {
                            match envelope.event {
                                Event::Result(result) => result.final_text,
                                _ => None,
                            }
                        });
                        Ok::<_, Infallible>(final_text.unwrap_or_default())
                    }
                });
            let verdict = review.await?;
            Ok(verdict.contains("LGTM"))
        }
    };

    let first = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert!(first.unwrap());
    let recorder = Recorder::default();
    let resumed = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert!(resumed.unwrap());
    assert_eq!(fixture.provider.requests().len(), 1);
    let replayed = &recorder.events()[0];
    assert_eq!(replayed.result_text.as_deref(), Some("\"LGTM\""));
}
