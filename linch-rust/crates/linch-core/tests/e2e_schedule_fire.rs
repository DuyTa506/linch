//! A run a worker starts for a schedule carries the fire in its first
//! events, in the same write as its `init`; any other run is unchanged.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::kinds::ScheduleFireEvent;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{Agent, Event, PermissionMode, RunId, RunOptions, RunStore, StoreError};

fn fire() -> ScheduleFireEvent {
    ScheduleFireEvent {
        schedule_id: "nightly".into(),
        fire_at_ms: 1_762_400_000_000,
    }
}

fn scheduled() -> RunOptions {
    let mut options = RunOptions::new("s1");
    options.schedule = Some(fire());
    options.run_id = Some(RunId::new("sched-run"));
    options
}

fn make<S: RunStore + linch_core::SessionStore + 'static>(stores: Arc<S>) -> Agent {
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        stores,
    )
}

#[tokio::test]
async fn a_scheduled_run_names_its_fire_right_after_init() {
    let stores = Arc::new(Stores::default());
    let envelopes = finish(make(stores.clone()).run("go", scheduled())).await;

    assert_eq!(
        kinds(&envelopes)[..3],
        ["init", "schedule_fire", "user"],
        "the fire follows init and precedes the prompt"
    );
    let Event::ScheduleFire(seen) = &envelopes[1].event else {
        panic!("not a schedule_fire: {:?}", envelopes[1].event);
    };
    assert_eq!(*seen, fire());
    assert_eq!(envelopes[1].seq, 2);
    let wire = envelopes[1].event.to_value().unwrap();
    assert_eq!(
        wire,
        serde_json::json!({"type": "schedule_fire", "schedule_id": "nightly",
                           "fire_at_ms": 1_762_400_000_000_u64})
    );
    // It is durable, so a reader of `run.events` finds it too.
    let stored = stores
        .runs
        .events(NS, &envelopes[0].run_id, 0)
        .await
        .unwrap();
    assert_eq!(kinds(&stored)[1], "schedule_fire");
}

#[tokio::test]
async fn a_run_without_a_schedule_and_its_writes_are_unchanged() {
    let plain = Arc::new(Stores::default());
    let envelopes = finish(make(plain.clone()).run("go", RunOptions::new("s1"))).await;
    assert!(
        !kinds(&envelopes).contains(&"schedule_fire".to_owned()),
        "{:?}",
        kinds(&envelopes)
    );
    let fired = Arc::new(Stores::default());
    finish(make(fired.clone()).run("go", scheduled())).await;
    // The fire rides the create write: no write is added.
    assert_eq!(
        plain.log.lock().unwrap().len(),
        fired.log.lock().unwrap().len()
    );
}

/// Crash at every write of a scheduled run, then continue it: its events
/// hold the fire exactly once, at seq 2, with no gap.
#[tokio::test]
async fn a_crash_at_any_write_keeps_the_fire_once() {
    let writes = {
        let stores = Arc::new(Stores::default());
        finish(make(stores.clone()).run("go", scheduled())).await;
        stores.log.lock().unwrap().len()
    };
    assert!(writes >= 5, "{writes}");
    for n in 1..=writes as u64 {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = make(faulty).run("go", scheduled());
            let run_id: RunId = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            assert!(error.is_some(), "{context}: the run must stop");
            let clean = make(stores.clone());
            match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => {
                    finish(clean.resume(run_id.clone(), RunOptions::new("s1"))).await;
                }
                Err(StoreError::NotFound { .. }) => {
                    finish(clean.run("go", scheduled())).await;
                }
                Err(error) => panic!("{context}: {error}"),
            }
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let fires: Vec<u64> = (events.iter())
                .filter(|e| matches!(e.event, Event::ScheduleFire(_)))
                .map(|e| e.seq)
                .collect();
            assert_eq!(fires, [2], "{context}: {:?}", kinds(&events));
            let seqs: Vec<u64> = events.iter().map(|e| e.seq).collect();
            assert_eq!(seqs, (1..=events.len() as u64).collect::<Vec<_>>());
        }
    }
}
