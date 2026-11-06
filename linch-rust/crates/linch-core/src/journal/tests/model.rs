//! Model check: random stage/save/flush sequences only ever commit a state
//! that writing every step through at once would also have produced.

use proptest::prelude::*;

use super::*;

#[derive(Debug, Clone, Copy)]
enum Op {
    /// 0: durable and delivered, 1: journal-only, 2: stream-only.
    Stage(u8),
    /// 0..4 held phases, 4..7 write-through phases.
    Save(u8),
    Flush,
    FlushCheckpoint,
}

fn op() -> impl Strategy<Value = Op> {
    prop_oneof![
        3 => (0u8..3).prop_map(Op::Stage),
        2 => (0u8..7).prop_map(Op::Save),
        1 => Just(Op::Flush),
        1 => Just(Op::FlushCheckpoint),
    ]
}

fn event(kind: u8, i: u64) -> Event {
    match kind {
        0 => durable(i),
        1 => receipt(&i.to_string(), CallDisposition::Accepted),
        _ => progress(),
    }
}

/// A checkpoint unique to op `i`.
fn saved(kind: u8, i: u64) -> Checkpoint {
    let turn = u32::try_from(i).unwrap();
    let message = Message::user_text(i.to_string());
    let phase = match kind {
        0 => Phase::ProviderPending { attempt: 1 },
        1 => Phase::CompactionPending { attempt: 1 },
        2 => Phase::ToolBatch {
            decisions: Vec::new(),
            after_seq: i,
        },
        3 => Phase::ToolResults { message },
        4 => Phase::AssistantReceived {
            message,
            stop_reason: StopReason::EndTurn,
        },
        5 => Phase::Compacted {
            summary: message,
            keep_from: i,
        },
        _ => Phase::Started { prompt: message },
    };
    checkpoint(turn, i, phase)
}

async fn check(ops: Vec<Op>) {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    // Write-through reference: every step lands at once, so its states are
    // (durable events so far, latest checkpoint) after each step.
    let mut staged: Vec<Event> = Vec::new();
    let mut latest = started();
    let mut states = vec![(0, latest.clone())];
    let mut delivered: Vec<Envelope> = Vec::new();
    for (i, op) in (1..).zip(ops) {
        match op {
            Op::Stage(kind) => {
                let event = event(kind, i);
                let now = journal.stage(event.clone());
                if kind == 2 {
                    assert_eq!(now.map(|e| e.seq), Some(journal.durable_seq()));
                } else {
                    assert!(now.is_none(), "durable events wait for a write");
                    staged.push(event);
                    states.push((staged.len() as u64, latest.clone()));
                }
            }
            Op::Save(kind) => {
                latest = saved(kind, i);
                states.push((staged.len() as u64, latest.clone()));
                delivered.extend(journal.save(latest.clone()).await.unwrap());
            }
            Op::Flush => delivered.extend(journal.flush().await.unwrap()),
            Op::FlushCheckpoint => delivered.extend(journal.flush_checkpoint().await.unwrap()),
        }
        let record = store.load(NS, &run_id()).await.unwrap();
        let state = (record.last_seq, record.checkpoint);
        assert!(
            states.contains(&state),
            "not a write-through state: {state:?}"
        );
        assert_eq!(state.0, journal.durable_seq());
        assert!(
            delivered.iter().all(|e| e.seq <= state.0),
            "delivered early"
        );
    }

    delivered.extend(journal.flush().await.unwrap());
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!(record.checkpoint, latest, "the latest checkpoint lands");
    let stored = store.events(NS, &run_id(), 0).await.unwrap();
    let numbered = (1..).zip(staged);
    let expected: Vec<(u64, Event)> = numbered.collect();
    let actual: Vec<(u64, Event)> = stored.into_iter().map(|e| (e.seq, e.event)).collect();
    assert_eq!(actual, expected, "every durable event lands once, in order");
    let visible: Vec<(u64, Event)> = (expected.into_iter())
        .filter(|(_, e)| e.persistence() == Persistence::Both)
        .collect();
    let seen: Vec<(u64, Event)> = delivered.into_iter().map(|e| (e.seq, e.event)).collect();
    assert_eq!(
        seen, visible,
        "the caller sees each delivered event once, in order"
    );
}

proptest! {
    #[test]
    fn every_committed_state_is_a_write_through_state(
        ops in proptest::collection::vec(op(), 0..40),
    ) {
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        runtime.block_on(check(ops));
    }
}
