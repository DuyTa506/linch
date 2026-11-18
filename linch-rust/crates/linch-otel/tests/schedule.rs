//! A run a schedule fire started carries the schedule id on its spans.

mod common;

use common::*;
use linch_core::RunOptions;
use linch_core::events::kinds::ScheduleFireEvent;

const SPANS: [&str; 4] = [
    "invoke_agent",
    "step",
    "chat scripted-model",
    "execute_tool Read",
];

fn fire() -> ScheduleFireEvent {
    ScheduleFireEvent {
        schedule_id: "nightly".into(),
        fire_at_ms: 1_762_400_000_000,
    }
}

#[tokio::test]
async fn every_span_of_a_scheduled_run_carries_the_schedule_id() {
    let sdk = Sdk::new();
    let turns = vec![tool_turn(0, 1, "Read"), text_turn("done")];
    let agent = agent(turns, vec![Echo::new("Read", linch_core::ToolScope::Read)]);
    let mut options = RunOptions::new("s1");
    options.schedule = Some(fire());
    let mut observer = sdk.observer();
    observe(agent.run("go", options), &mut observer).await;
    drop(observer);

    let spans = sdk.spans();
    for name in SPANS {
        let found = all(&spans, name);
        assert!(!found.is_empty(), "no {name} span");
        for span in found {
            assert_eq!(
                text(span, "linch.schedule.id").as_deref(),
                Some("nightly"),
                "{name}"
            );
        }
    }
    // The fire time is on the run span, once.
    let run = span(&spans, "invoke_agent");
    assert_eq!(
        int(run, "linch.schedule.fire_at_ms"),
        Some(1_762_400_000_000)
    );
    for step in all(&spans, "step") {
        assert_eq!(int(step, "linch.schedule.fire_at_ms"), None);
    }
}

#[tokio::test]
async fn a_run_without_a_schedule_has_no_schedule_attribute() {
    let sdk = Sdk::new();
    let agent = agent(vec![text_turn("hello")], vec![]);
    let mut observer = sdk.observer();
    observe(agent.run("hi", RunOptions::new("s1")), &mut observer).await;
    drop(observer);

    for span in sdk.spans() {
        for key in ["linch.schedule.id", "linch.schedule.fire_at_ms"] {
            assert!(attr(&span, key).is_none(), "{} has {key}", span.name);
        }
    }
}
