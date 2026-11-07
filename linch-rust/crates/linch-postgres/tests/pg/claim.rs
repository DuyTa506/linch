//! Concurrent claims: each claim fences the one before it.

use linch_core::{Commit, RunId, RunStore, StoreError};
use linch_postgres::PgRunStore;

use crate::common::{TestDb, envelope, new_run};

fn one_event(run_id: &RunId) -> Commit {
    Commit {
        events: vec![envelope(run_id, 1)],
        ..Commit::default()
    }
}

/// Two owners claim one run at once: both get a lease, the epochs are 1 and
/// 2, and only the epoch-2 lease can commit.
#[tokio::test]
async fn concurrent_claims_fence_the_older_lease() {
    let Some(db) = TestDb::migrated("claim").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    for i in 0..10 {
        let run = new_run("ns", &format!("r{i}"), 0);
        let id = run.run_id.clone();
        store.create(run).await.unwrap();
        let (a, b) = tokio::join!(store.claim("ns", &id, "a"), store.claim("ns", &id, "b"));
        let (a, b) = (a.unwrap(), b.unwrap());
        let (stale, current) = if a.epoch < b.epoch { (a, b) } else { (b, a) };
        assert_eq!((stale.epoch, current.epoch), (1, 2), "no lost update");
        let result = store.commit(&stale, one_event(&id)).await;
        assert!(
            matches!(result, Err(StoreError::LeaseLost { epoch: 1, .. })),
            "{result:?}"
        );
        store.commit(&current, one_event(&id)).await.unwrap();
        let record = store.load("ns", &id).await.unwrap();
        assert_eq!((record.epoch, record.last_seq), (2, 1));
    }
    db.finish().await;
}

/// A commit racing a new claim either lands before the claim or is fenced;
/// it never lands under the newer epoch.
#[tokio::test]
async fn a_commit_racing_a_claim_lands_whole_or_not_at_all() {
    let Some(db) = TestDb::migrated("claim_race").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let mut outcomes = [0; 2];
    for i in 0..20 {
        let run = new_run("ns", &format!("r{i}"), 0);
        let id = run.run_id.clone();
        store.create(run).await.unwrap();
        let first = store.claim("ns", &id, "a").await.unwrap();
        let (commit, claim) = tokio::join!(
            store.commit(&first, one_event(&id)),
            store.claim("ns", &id, "b")
        );
        assert_eq!(claim.unwrap().epoch, 2);
        let events = store.events("ns", &id, 0).await.unwrap();
        match commit {
            Ok(()) => {
                assert_eq!(events.len(), 1);
                outcomes[0] += 1;
            }
            Err(StoreError::LeaseLost { epoch: 1, .. }) => {
                assert!(events.is_empty());
                outcomes[1] += 1;
            }
            Err(other) => panic!("unexpected {other:?}"),
        }
    }
    eprintln!(
        "commit landed {} times, fenced {} times",
        outcomes[0], outcomes[1]
    );
    db.finish().await;
}
