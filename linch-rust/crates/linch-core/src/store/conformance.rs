//! Store conformance suite shared by every backend.
//!
//! Every [`RunStore`] and [`SessionStore`] must pass [`run_store_suite`],
//! [`run_admission_suite`] and [`session_store_suite`], and every
//! [`TaskStore`](crate::store::TaskStore) [`task_store_suite`]. A failed rule panics
//! with a message naming it, so call the suites from a test. Each check uses
//! its own namespace under `conformance/`; run the suites on a store that
//! holds nothing there.
//!
//! The run lifecycle the suite pins down:
//! - `create` stores a `pending` run at epoch 0 with its first events;
//! - `claim` is the only way to `running`, and adds 1 to the epoch;
//! - `commit` is accepted only on a `running` run, under the current epoch;
//! - a terminal status is final, and a rejected commit changes nothing;
//! - a session holds at most one run that is not terminal, per namespace
//!   ([`run_admission_suite`]).

mod admission;
mod fixtures;
mod tasks;

pub use admission::run_admission_suite;
use fixtures::{
    checkpoint, checkpoint_at, commit_shapes, entry, envelope, lease_for, new_run, same_record,
};
pub use tasks::task_store_suite;

use crate::ids::{RunId, SessionId};
use crate::run::RunStatus;
use crate::store::{Commit, Lease, RunStore, SessionStore, StoreError};

/// Check every [`RunStore`] rule. Panics on the first rule that fails.
pub async fn run_store_suite(store: &dyn RunStore) {
    create_and_load(store).await;
    namespaces_are_isolated(store).await;
    commits_continue_the_seq(store).await;
    stale_leases_are_fenced(store).await;
    statuses_follow_the_lifecycle(store).await;
}

/// Check every [`SessionStore`] rule. Panics on the first rule that fails.
pub async fn session_store_suite(store: &dyn SessionStore) {
    let ns = "conformance/session";
    let id = SessionId::new("s1");
    assert!(
        matches!(store.load(ns, &id).await, Err(StoreError::NotFound { .. })),
        "load of a missing session is NotFound"
    );
    assert!(
        matches!(
            store.append(ns, &id, 0, vec![entry("x")]).await,
            Err(StoreError::NotFound { .. })
        ),
        "append to a missing session is NotFound"
    );
    store.create(ns, &id).await.expect("create a session");
    assert!(
        matches!(
            store.create(ns, &id).await,
            Err(StoreError::Conflict { .. })
        ),
        "a second create is Conflict"
    );
    assert!(
        store.load(ns, &id).await.unwrap().is_empty(),
        "new log is empty"
    );
    let len = store.append(ns, &id, 0, vec![entry("a"), entry("b")]).await;
    assert_eq!(len.unwrap(), 2, "append returns the new length");
    for stale in [0, 1, 3] {
        assert!(
            matches!(
                store.append(ns, &id, stale, vec![entry("c")]).await,
                Err(StoreError::Conflict { .. })
            ),
            "append at length {stale} while the log has 2 is Conflict"
        );
    }
    assert_eq!(store.append(ns, &id, 2, vec![entry("c")]).await.unwrap(), 3);
    let log = store.load(ns, &id).await.unwrap();
    assert_eq!(log, vec![entry("a"), entry("b"), entry("c")], "order kept");

    let other = "conformance/session-other";
    assert!(
        matches!(
            store.load(other, &id).await,
            Err(StoreError::NotFound { .. })
        ),
        "a session is invisible from another namespace"
    );
    store
        .create(other, &id)
        .await
        .expect("same id, other namespace");
    assert!(store.load(other, &id).await.unwrap().is_empty());
    assert_eq!(store.load(ns, &id).await.unwrap().len(), 3);
}

async fn create_and_load(store: &dyn RunStore) {
    let ns = "conformance/create";
    let run = new_run(ns, "r1", 2);
    let record = store.create(run.clone()).await.expect("create a run");
    assert_eq!(record.status, RunStatus::Pending, "a new run is pending");
    assert_eq!(record.epoch, 0, "a new run is at epoch 0");
    assert_eq!(record.last_seq, 2, "last_seq counts the first events");
    assert_eq!(record.checkpoint, run.checkpoint);
    assert_eq!(record.contract, run.contract);
    assert_eq!(record.session_id, run.session_id);
    assert_eq!(record.parent_run_id, run.parent_run_id);
    same_record(&store.load(ns, &run.run_id).await.unwrap(), &record);
    assert_eq!(store.events(ns, &run.run_id, 0).await.unwrap(), run.events);
    assert_eq!(
        store.events(ns, &run.run_id, 1).await.unwrap(),
        run.events[1..]
    );
    assert!(store.events(ns, &run.run_id, 9).await.unwrap().is_empty());
    assert!(
        matches!(store.create(run).await, Err(StoreError::Conflict { .. })),
        "a second create is Conflict"
    );

    let mut gap = new_run(ns, "r2", 2);
    gap.events.remove(0);
    assert!(
        matches!(
            store.create(gap).await,
            Err(StoreError::SeqGap {
                expected: 1,
                got: 2
            })
        ),
        "first events must start at seq 1"
    );
    let mut foreign = new_run(ns, "r3", 1);
    foreign.events[0].run_id = RunId::new("elsewhere");
    assert!(
        matches!(
            store.create(foreign).await,
            Err(StoreError::Conflict { .. })
        ),
        "first events must belong to the run"
    );
    for id in ["r2", "r3"] {
        let missing = store.load(ns, &RunId::new(id)).await;
        assert!(matches!(missing, Err(StoreError::NotFound { .. })), "{id}");
    }

    let missing = RunId::new("missing");
    let lease = lease_for(ns, "missing", 1);
    assert!(matches!(
        store.load(ns, &missing).await,
        Err(StoreError::NotFound { .. })
    ));
    assert!(matches!(
        store.events(ns, &missing, 0).await,
        Err(StoreError::NotFound { .. })
    ));
    assert!(matches!(
        store.claim(ns, &missing, "w").await,
        Err(StoreError::NotFound { .. })
    ));
    let commit = store.commit(&lease, Commit::default()).await;
    assert!(matches!(commit, Err(StoreError::NotFound { .. })));
}

async fn namespaces_are_isolated(store: &dyn RunStore) {
    let (a, b) = ("conformance/ns-a", "conformance/ns-b");
    let id = RunId::new("r1");
    store.create(new_run(a, "r1", 1)).await.unwrap();
    assert!(
        matches!(store.load(b, &id).await, Err(StoreError::NotFound { .. })),
        "a run is invisible from another namespace"
    );
    assert!(matches!(
        store.claim(b, &id, "w").await,
        Err(StoreError::NotFound { .. })
    ));
    store
        .create(new_run(b, "r1", 3))
        .await
        .expect("same id, other namespace");
    let lease_a = store.claim(a, &id, "w").await.unwrap();
    let commit = Commit {
        events: vec![envelope("r1", 2)],
        ..Commit::default()
    };
    store.commit(&lease_a, commit).await.unwrap();
    assert_eq!(store.load(a, &id).await.unwrap().last_seq, 2);
    let in_b = store.load(b, &id).await.unwrap();
    assert_eq!((in_b.last_seq, in_b.epoch), (3, 0), "b is untouched");
    assert_eq!(store.events(b, &id, 0).await.unwrap().len(), 3);
}

async fn commits_continue_the_seq(store: &dyn RunStore) {
    let ns = "conformance/seq";
    let id = RunId::new("r1");
    store.create(new_run(ns, "r1", 1)).await.unwrap();
    let lease = store.claim(ns, &id, "w").await.unwrap();
    let checkpoint = checkpoint(1);
    let commit = Commit {
        events: vec![envelope("r1", 2), envelope("r1", 3)],
        checkpoint: Some(checkpoint.clone()),
        status: None,
    };
    store
        .commit(&lease, commit)
        .await
        .expect("contiguous commit");
    let record = store.load(ns, &id).await.unwrap();
    assert_eq!((record.last_seq, &record.checkpoint), (3, &checkpoint));
    let seqs: Vec<u64> = (store.events(ns, &id, 1).await.unwrap().iter())
        .map(|e| e.seq)
        .collect();
    assert_eq!(seqs, [2, 3]);

    for (events, expected, got) in [
        (vec![envelope("r1", 5)], 4, 5),
        (vec![envelope("r1", 3)], 4, 3),
        (vec![envelope("r1", 4), envelope("r1", 6)], 5, 6),
    ] {
        let commit = Commit {
            events,
            checkpoint: Some(checkpoint_at(9)),
            status: Some(RunStatus::Completed),
        };
        let result = store.commit(&lease, commit).await;
        assert!(
            matches!(result, Err(StoreError::SeqGap { expected: e, got: g }) if e == expected && g == got),
            "seq gap {expected}/{got}: {result:?}"
        );
    }
    let foreign = Commit {
        events: vec![envelope("elsewhere", 4)],
        ..Commit::default()
    };
    let result = store.commit(&lease, foreign).await;
    assert!(
        matches!(result, Err(StoreError::Conflict { .. })),
        "foreign event"
    );
    same_record(&store.load(ns, &id).await.unwrap(), &record);
    assert_eq!(store.events(ns, &id, 0).await.unwrap().len(), 3);
}

async fn stale_leases_are_fenced(store: &dyn RunStore) {
    let ns = "conformance/lease";
    let id = RunId::new("r1");
    store.create(new_run(ns, "r1", 1)).await.unwrap();
    let first = store.claim(ns, &id, "w1").await.unwrap();
    assert_eq!((first.epoch, first.owner.as_str()), (1, "w1"));
    let second = store.claim(ns, &id, "w2").await.unwrap();
    assert_eq!(second.epoch, 2, "each claim adds 1 to the epoch");
    let before = store.load(ns, &id).await.unwrap();
    for commit in commit_shapes(2) {
        let result = store.commit(&first, commit).await;
        assert!(
            matches!(result, Err(StoreError::LeaseLost { epoch: 1, .. })),
            "a stale lease is rejected on every commit shape: {result:?}"
        );
    }
    let future = Lease {
        epoch: 3,
        ..second.clone()
    };
    let result = store.commit(&future, Commit::default()).await;
    assert!(matches!(
        result,
        Err(StoreError::LeaseLost { epoch: 3, .. })
    ));
    let other_ns = Lease {
        namespace: "conformance/lease-other".into(),
        ..second.clone()
    };
    let result = store.commit(&other_ns, Commit::default()).await;
    assert!(matches!(result, Err(StoreError::NotFound { .. })));
    same_record(&store.load(ns, &id).await.unwrap(), &before);
    for commit in commit_shapes(2) {
        store.commit(&second, commit).await.expect("current lease");
    }
}

async fn statuses_follow_the_lifecycle(store: &dyn RunStore) {
    let ns = "conformance/status";
    let id = RunId::new("r1");
    store.create(new_run(ns, "r1", 1)).await.unwrap();
    let pending = lease_for(ns, "r1", 0);
    let result = store.commit(&pending, Commit::default()).await;
    assert!(
        matches!(
            result,
            Err(StoreError::InvalidTransition {
                from: RunStatus::Pending,
                ..
            })
        ),
        "a pending run takes no commit before a claim: {result:?}"
    );
    let lease = store.claim(ns, &id, "w").await.unwrap();
    assert_eq!(
        store.load(ns, &id).await.unwrap().status,
        RunStatus::Running
    );
    rejects(store, &lease, Some(RunStatus::Pending), RunStatus::Running).await;
    commit_status(store, &lease, RunStatus::Running).await;
    commit_status(store, &lease, RunStatus::AwaitingApproval).await;
    for to in [None, Some(RunStatus::Running), Some(RunStatus::Completed)] {
        rejects(store, &lease, to, RunStatus::AwaitingApproval).await;
    }
    let lease = store.claim(ns, &id, "w").await.unwrap();
    assert_eq!(lease.epoch, 2);
    let record = store.load(ns, &id).await.unwrap();
    assert_eq!(record.status, RunStatus::Running, "claim resumes awaiting");
    commit_status(store, &lease, RunStatus::AwaitingInput).await;
    let lease = store.claim(ns, &id, "w").await.unwrap();
    commit_status(store, &lease, RunStatus::Completed).await;
    for to in [None, Some(RunStatus::Running), Some(RunStatus::Failed)] {
        rejects(store, &lease, to, RunStatus::Completed).await;
    }
    let claim = store.claim(ns, &id, "w").await;
    assert!(
        matches!(
            claim,
            Err(StoreError::InvalidTransition {
                from: RunStatus::Completed,
                ..
            })
        ),
        "a terminal run cannot be claimed: {claim:?}"
    );

    for (run, terminal) in [("r2", RunStatus::Failed), ("r3", RunStatus::Aborted)] {
        store.create(new_run(ns, run, 1)).await.unwrap();
        let lease = store.claim(ns, &RunId::new(run), "w").await.unwrap();
        commit_status(store, &lease, terminal).await;
        rejects(store, &lease, None, terminal).await;
    }
}

async fn commit_status(store: &dyn RunStore, lease: &Lease, status: RunStatus) {
    let commit = Commit {
        status: Some(status),
        ..Commit::default()
    };
    store.commit(lease, commit).await.expect("allowed status");
    let record = store.load(&lease.namespace, &lease.run_id).await.unwrap();
    assert_eq!(record.status, status);
}

async fn rejects(store: &dyn RunStore, lease: &Lease, to: Option<RunStatus>, from: RunStatus) {
    let before = store.load(&lease.namespace, &lease.run_id).await.unwrap();
    let commit = Commit {
        events: vec![envelope(lease.run_id.as_str(), before.last_seq + 1)],
        checkpoint: Some(checkpoint_at(7)),
        status: to,
    };
    let result = store.commit(lease, commit).await;
    assert!(
        matches!(result, Err(StoreError::InvalidTransition { from: f, .. }) if f == from),
        "{from:?} -> {to:?} is rejected: {result:?}"
    );
    same_record(
        &store.load(&lease.namespace, &lease.run_id).await.unwrap(),
        &before,
    );
}
