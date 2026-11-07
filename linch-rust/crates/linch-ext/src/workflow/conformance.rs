//! Journal store conformance suite shared by every backend.
//!
//! Every [`JournalStore`] must pass [`journal_store_suite`]. A failed rule
//! panics with a message naming it, so call the suite from a test. Each
//! check uses its own namespace under `conformance/`; run the suite on a
//! store that holds nothing there.
//!
//! The rules it pins down:
//! - `claim` creates a `running` workflow at epoch 1, and every later claim
//!   adds 1 to the epoch, resets the status to `running` and keeps the
//!   records;
//! - `append` and `set_status` land only under the current epoch, and a
//!   rejected write changes nothing;
//! - a `(call_key, occurrence)` is recorded at most once;
//! - values and statuses round-trip exactly, and namespaces never share
//!   workflows.

use serde_json::json;

use super::journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowStatus,
};

/// Check every [`JournalStore`] rule. Panics on the first rule that fails.
pub async fn journal_store_suite(store: &dyn JournalStore) {
    claim_creates_and_bumps_the_epoch(store).await;
    records_round_trip(store).await;
    stale_leases_are_fenced(store).await;
    statuses_round_trip(store).await;
    namespaces_are_isolated(store).await;
}

fn record(call_key: &str, occurrence: u64) -> JournalRecord {
    JournalRecord {
        call_key: call_key.into(),
        occurrence,
        value: json!({"zeta": [1, 2.5, "x"], "alpha": {"b": null, "a": true}}),
    }
}

async fn claim_creates_and_bumps_the_epoch(store: &dyn JournalStore) {
    let ns = "conformance/claim";
    let missing = store.load(ns, "w1").await.expect("load");
    assert!(
        missing.is_none(),
        "load of a never-claimed workflow is None"
    );
    let first = store.claim(ns, "w1").await.expect("first claim");
    assert_eq!(first.epoch, 1, "the first claim gives epoch 1");
    let run = store.load(ns, "w1").await.expect("load").expect("created");
    assert_eq!(run.status, WorkflowStatus::Running, "a claim sets running");
    assert_eq!(run.epoch, 1, "load reports the claimed epoch");
    assert!(run.records.is_empty(), "a new workflow has no records");
    let second = store.claim(ns, "w1").await.expect("second claim");
    assert_eq!(second.epoch, 2, "each claim adds 1 to the epoch");
}

async fn records_round_trip(store: &dyn JournalStore) {
    let ns = "conformance/records";
    let lease = store.claim(ns, "w1").await.expect("claim");
    for (key, occurrence) in [("b", 0), ("a", 1), ("a", 0)] {
        let appended = store.append(&lease, record(key, occurrence)).await;
        appended.expect("append under the current lease");
    }
    let duplicate = store.append(&lease, record("a", 1)).await;
    assert!(
        matches!(duplicate, Err(JournalStoreError::Conflict { .. })),
        "a second record of one (call_key, occurrence) is Conflict"
    );
    let run = store.load(ns, "w1").await.expect("load").expect("exists");
    let keys: Vec<_> = (run.records.iter())
        .map(|r| (r.call_key.as_str(), r.occurrence))
        .collect();
    assert_eq!(
        keys,
        [("a", 0), ("a", 1), ("b", 0)],
        "records are ordered by (call_key, occurrence)"
    );
    assert_eq!(run.records[0], record("a", 0), "a value round-trips");
    let again = store.claim(ns, "w1").await.expect("claim again");
    let run = store.load(ns, "w1").await.expect("load").expect("exists");
    assert_eq!(run.records.len(), 3, "a claim keeps the records");
    let appended = store.append(&again, record("c", 0)).await;
    appended.expect("the new lease appends");
}

async fn stale_leases_are_fenced(store: &dyn JournalStore) {
    let ns = "conformance/fence";
    let old = store.claim(ns, "w1").await.expect("claim");
    let new = store.claim(ns, "w1").await.expect("claim again");
    let appended = store.append(&old, record("k", 0)).await;
    assert!(
        matches!(appended, Err(JournalStoreError::LeaseLost { epoch: 1, .. })),
        "an append under a fenced lease is LeaseLost"
    );
    let status = store.set_status(&old, WorkflowStatus::Completed).await;
    assert!(
        matches!(status, Err(JournalStoreError::LeaseLost { .. })),
        "a status write under a fenced lease is LeaseLost"
    );
    let run = store.load(ns, "w1").await.expect("load").expect("exists");
    assert_eq!(
        run.status,
        WorkflowStatus::Running,
        "the fenced write did nothing"
    );
    assert!(run.records.is_empty(), "the fenced append did nothing");
    let unknown = JournalLease {
        workflow_id: "w2".into(),
        ..new
    };
    let appended = store.append(&unknown, record("k", 0)).await;
    assert!(
        matches!(appended, Err(JournalStoreError::LeaseLost { .. })),
        "a lease on a workflow that does not exist is LeaseLost"
    );
}

async fn statuses_round_trip(store: &dyn JournalStore) {
    let ns = "conformance/status";
    let lease = store.claim(ns, "w1").await.expect("claim");
    let interrupt = Interrupt {
        key: "approve".into(),
        payload: json!({"plan": ["a", "b"], "n": 1.5}),
    };
    for status in [
        WorkflowStatus::AwaitingInput(interrupt),
        WorkflowStatus::Failed,
        WorkflowStatus::Completed,
    ] {
        let set = store.set_status(&lease, status.clone()).await;
        set.expect("set the status under the current lease");
        let run = store.load(ns, "w1").await.expect("load").expect("exists");
        assert_eq!(run.status, status, "{} round-trips", status.name());
    }
    let lease = store.claim(ns, "w1").await.expect("claim a completed run");
    let suspended = WorkflowStatus::AwaitingInput(Interrupt {
        key: "k".into(),
        payload: json!(null),
    });
    let set = store.set_status(&lease, suspended).await;
    set.expect("suspend");
    store.claim(ns, "w1").await.expect("claim a suspended run");
    let run = store.load(ns, "w1").await.expect("load").expect("exists");
    assert_eq!(
        run.status,
        WorkflowStatus::Running,
        "a claim clears the pending interrupt"
    );
}

async fn namespaces_are_isolated(store: &dyn JournalStore) {
    let (a, b) = ("conformance/ns-a", "conformance/ns-b");
    let lease = store.claim(a, "w1").await.expect("claim in a");
    store.append(&lease, record("k", 0)).await.expect("append");
    assert!(
        store.load(b, "w1").await.expect("load").is_none(),
        "a workflow is invisible from another namespace"
    );
    let other = store.claim(b, "w1").await.expect("claim in b");
    assert_eq!(other.epoch, 1, "namespaces keep separate epochs");
    let run = store.load(a, "w1").await.expect("load").expect("exists");
    assert_eq!(run.epoch, 1, "a claim in b leaves a alone");
}
