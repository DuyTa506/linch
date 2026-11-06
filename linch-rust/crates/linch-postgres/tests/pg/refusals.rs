//! Refused writes: which refusal wins when several apply, the exact error,
//! and that a refused write changes nothing, alone or racing others.

use std::fmt::Debug;

use linch_core::{
    Commit, Envelope, Lease, LogEntry, Message, MessageEntry, RunId, RunStatus, RunStore,
    SessionId, SessionStore, StoreError,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use tokio::task::JoinSet;

use crate::common::{TestDb, checkpoint, envelope, new_run};

/// The text of a refusal; a backend error or an accepted write fails.
fn refusal<T: Debug>(result: Result<T, StoreError>) -> String {
    match result {
        Err(StoreError::Backend(error)) => panic!("a backend error: {error:?}"),
        Err(error) => error.to_string(),
        Ok(value) => panic!("accepted: {value:?}"),
    }
}

fn commit(events: Vec<Envelope>, status: Option<RunStatus>) -> Commit {
    Commit {
        events,
        checkpoint: Some(checkpoint(9)),
        status,
    }
}

fn entry(text: &str) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Message::user_text(text).into(),
        visible: true,
        historical: true,
    })
}

/// Refusals keep linch-core's order: not found, lease, status, then each
/// event's run id and seq in turn.
#[tokio::test]
async fn the_first_refusal_in_core_order_wins() {
    let Some(db) = TestDb::migrated("refusal_order").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let id = RunId::new("r1");
    let foreign = RunId::new("elsewhere");
    store.create(new_run("ns", "r1", 1)).await.unwrap();
    let stale = store.claim("ns", &id, "a").await.unwrap();
    let lease = store.claim("ns", &id, "b").await.unwrap();
    let before = store.load("ns", &id).await.unwrap();

    let missing = Lease {
        run_id: RunId::new("r9"),
        ..lease.clone()
    };
    let cases: [(&Lease, Commit, &str); 9] = [
        (
            &missing,
            commit(vec![envelope(&foreign, 7)], Some(RunStatus::Pending)),
            "run r9 not found in namespace ns",
        ),
        (
            &missing,
            commit(vec![envelope(&missing.run_id, 1)], None),
            "run r9 not found in namespace ns",
        ),
        (
            &stale,
            commit(vec![envelope(&foreign, 7)], Some(RunStatus::Pending)),
            "lease on run r1 lost (epoch 1)",
        ),
        (
            &stale,
            commit(vec![envelope(&id, 5)], None),
            "lease on run r1 lost (epoch 1)",
        ),
        (
            &lease,
            commit(vec![envelope(&foreign, 7)], Some(RunStatus::Pending)),
            "invalid run status transition Running -> Pending",
        ),
        (
            &lease,
            commit(vec![envelope(&id, 5), envelope(&foreign, 6)], None),
            "event seq gap: expected 2, got 5",
        ),
        (
            &lease,
            commit(vec![envelope(&id, 2), envelope(&foreign, 3)], None),
            "conflict: event for run elsewhere written to run r1",
        ),
        (
            &lease,
            commit(vec![envelope(&id, 2), envelope(&id, 4)], None),
            "event seq gap: expected 3, got 4",
        ),
        (
            &lease,
            commit(vec![envelope(&id, 3)], Some(RunStatus::Completed)),
            "event seq gap: expected 2, got 3",
        ),
    ];
    for (lease, commit, expected) in cases {
        assert_eq!(refusal(store.commit(lease, commit).await), expected);
    }
    let after = store.load("ns", &id).await.unwrap();
    assert_eq!(
        (after.status, after.epoch, after.last_seq, &after.checkpoint),
        (
            before.status,
            before.epoch,
            before.last_seq,
            &before.checkpoint
        ),
        "no refusal changed the run"
    );
    assert_eq!(store.events("ns", &id, 0).await.unwrap().len(), 1);

    // A suspended run refuses before its events are looked at.
    let suspend = commit(vec![envelope(&id, 2)], Some(RunStatus::AwaitingApproval));
    store.commit(&lease, suspend).await.unwrap();
    for events in [vec![envelope(&foreign, 9)], vec![envelope(&id, 3)]] {
        assert_eq!(
            refusal(store.commit(&lease, commit(events, None)).await),
            "invalid run status transition AwaitingApproval -> AwaitingApproval"
        );
    }
    assert_eq!(store.load("ns", &id).await.unwrap().last_seq, 2);
    db.finish().await;
}

#[tokio::test]
async fn a_refused_append_names_the_length_and_changes_nothing() {
    let Some(db) = TestDb::migrated("refusal_append").await else {
        return;
    };
    let store = PgSessionStore::new(db.pool.clone());
    let id = SessionId::new("s1");
    let missing = store.append("ns", &id, 0, vec![entry("a")]).await;
    assert_eq!(refusal(missing), "session s1 not found in namespace ns");
    store.create("ns", &id).await.unwrap();
    store
        .append("ns", &id, 0, vec![entry("a"), entry("b")])
        .await
        .unwrap();
    for (expected, entries) in [(1, vec![entry("c")]), (3, vec![]), (0, vec![entry("c")])] {
        let result = store.append("ns", &id, expected, entries).await;
        let message = format!("conflict: session s1 has 2 entries, expected {expected}");
        assert_eq!(refusal(result), message);
    }
    let other = store.append("other", &id, 2, vec![entry("c")]).await;
    assert_eq!(refusal(other), "session s1 not found in namespace other");
    assert_eq!(
        store.load("ns", &id).await.unwrap(),
        [entry("a"), entry("b")]
    );
    assert_eq!(store.append("ns", &id, 2, vec![]).await.unwrap(), 2);
    db.finish().await;
}

/// Commits under one lease racing for the same seq: one lands, every other
/// is refused with the seq the winner left.
#[tokio::test]
async fn racing_commits_at_one_seq_land_once() {
    let Some(db) = TestDb::migrated("refusal_race_commit").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    for round in 0..5 {
        let run = new_run("ns", &format!("r{round}"), 1);
        let id = run.run_id.clone();
        store.create(run).await.unwrap();
        let lease = store.claim("ns", &id, "w").await.unwrap();
        let mut commits = JoinSet::new();
        for _ in 0..4 {
            let (store, lease) = (store.clone(), lease.clone());
            let events = vec![envelope(&id, 2), envelope(&id, 3)];
            commits.spawn(async move { store.commit(&lease, commit(events, None)).await });
        }
        let mut landed = 0;
        while let Some(result) = commits.join_next().await {
            match result.unwrap() {
                Ok(()) => landed += 1,
                refused => assert_eq!(refusal(refused), "event seq gap: expected 4, got 2"),
            }
        }
        assert_eq!(landed, 1, "round {round}");
        let seqs: Vec<u64> = (store.events("ns", &id, 0).await.unwrap().iter())
            .map(|e| e.seq)
            .collect();
        assert_eq!(seqs, [1, 2, 3], "round {round}");
    }
    db.finish().await;
}

/// Appends racing at one length: one lands, every other is refused with
/// the length the winner left.
#[tokio::test]
async fn racing_appends_at_one_length_land_once() {
    let Some(db) = TestDb::migrated("refusal_race_append").await else {
        return;
    };
    let store = PgSessionStore::new(db.pool.clone());
    for round in 0..5 {
        let id = SessionId::new(format!("s{round}"));
        store.create("ns", &id).await.unwrap();
        let mut appends = JoinSet::new();
        for i in 0..4 {
            let (store, id) = (store.clone(), id.clone());
            let entries = vec![entry(&format!("{i}a")), entry(&format!("{i}b"))];
            appends.spawn(async move { store.append("ns", &id, 0, entries).await });
        }
        let mut landed = 0;
        while let Some(result) = appends.join_next().await {
            match result.unwrap() {
                Ok(len) => {
                    assert_eq!(len, 2);
                    landed += 1;
                }
                refused => {
                    let message = format!("conflict: session {id} has 2 entries, expected 0");
                    assert_eq!(refusal(refused), message);
                }
            }
        }
        assert_eq!(landed, 1, "round {round}");
        assert_eq!(store.load("ns", &id).await.unwrap().len(), 2);
    }
    db.finish().await;
}
