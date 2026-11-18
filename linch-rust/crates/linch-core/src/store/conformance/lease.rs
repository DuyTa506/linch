//! The lease rules of a [`RunStore`] whose claims expire.

use std::time::{Duration, Instant};

use super::commit_status;
use super::fixtures::new_run;
use crate::ids::RunId;
use crate::run::RunStatus;
use crate::store::{Commit, RunStore, StoreError};

/// Check the lease rules of a store whose claims expire, through two
/// handles `a` and `b` on one backend that are different lease holders
/// (two processes) and have the same [`RunStore::lease_ttl`]. Keep the TTL
/// short: the suite waits it out a few times. Panics on the first rule
/// that fails.
///
/// - a claim through another holder is refused with `RunLeased`, writing
///   nothing, while the run is `running` under a live lease;
/// - the holder's own claim fences its earlier lease out, as without leases;
/// - a renewal keeps the lease alive past its TTL; one under a fenced
///   lease is `LeaseLost`;
/// - a released, expired or suspended lease refuses nobody;
/// - `seize` takes the run over a live lease, and the fenced holder can
///   neither commit nor renew.
pub async fn run_lease_suite(a: &dyn RunStore, b: &dyn RunStore) {
    let ttl = a.lease_ttl().expect("a store whose claims expire");
    assert_eq!(b.lease_ttl(), Some(ttl), "both handles have one TTL");
    let ns = "conformance/lease-ttl";
    let run = |id: &str| {
        let id = RunId::new(id);
        async move {
            a.create(new_run(ns, id.as_str(), 1)).await.unwrap();
            id
        }
    };

    let id = run("live").await;
    let first = a.claim(ns, &id, "w1").await.unwrap();
    refused(b, ns, &id, "w1", ttl).await;
    assert_eq!(
        a.load(ns, &id).await.unwrap().epoch,
        1,
        "a refusal writes nothing"
    );
    let again = a
        .claim(ns, &id, "w1")
        .await
        .expect("the holder claims again");
    assert_eq!(again.epoch, 2, "and fences its earlier lease out");
    lost(a.renew(&first).await, "a renewal under a fenced lease");

    let id = run("renewed").await;
    let lease = a.claim(ns, &id, "w1").await.unwrap();
    let until = Instant::now() + ttl * 2;
    while Instant::now() < until {
        tokio::time::sleep(ttl / 4).await;
        a.renew(&lease).await.expect("the holder renews");
    }
    refused(b, ns, &id, "w1", ttl).await;

    let id = run("released").await;
    let lease = a.claim(ns, &id, "w1").await.unwrap();
    a.release(&lease).await.unwrap();
    b.claim(ns, &id, "w2")
        .await
        .expect("a released run is free");

    let id = run("expired").await;
    let lease = a.claim(ns, &id, "w1").await.unwrap();
    tokio::time::sleep(ttl + Duration::from_millis(100)).await;
    b.claim(ns, &id, "w2")
        .await
        .expect("an expired lease refuses nobody");
    lost(a.renew(&lease).await, "a renewal after another claim");

    let id = run("suspended").await;
    let lease = a.claim(ns, &id, "w1").await.unwrap();
    commit_status(a, &lease, RunStatus::AwaitingApproval).await;
    b.claim(ns, &id, "w2")
        .await
        .expect("a suspended run is free");

    let id = run("seized").await;
    let lease = a.claim(ns, &id, "w1").await.unwrap();
    let seized = b
        .seize(ns, &id, "w2")
        .await
        .expect("seize takes a live run");
    assert_eq!(seized.epoch, 2);
    lost(a.renew(&lease).await, "a renewal after a seize");
    let commit = a.commit(&lease, Commit::default()).await;
    assert!(
        matches!(commit, Err(StoreError::LeaseLost { epoch: 1, .. })),
        "a commit after a seize is LeaseLost: {commit:?}"
    );
    refused(a, ns, &id, "w2", ttl).await;
}

/// `store`'s claim on `id` is refused, naming `owner`.
async fn refused(store: &dyn RunStore, ns: &str, id: &RunId, owner: &str, ttl: Duration) {
    match store.claim(ns, id, "other").await {
        Err(StoreError::RunLeased {
            run_id,
            owner: holder,
            expires_in,
        }) => {
            assert_eq!((&run_id, holder.as_str()), (id, owner));
            assert!(expires_in <= ttl, "{expires_in:?} left of {ttl:?}");
        }
        other => panic!("a claim on a live lease of another holder is RunLeased: {other:?}"),
    }
}

fn lost(result: Result<(), StoreError>, what: &str) {
    assert!(
        matches!(result, Err(StoreError::LeaseLost { .. })),
        "{what} is LeaseLost: {result:?}"
    );
}
