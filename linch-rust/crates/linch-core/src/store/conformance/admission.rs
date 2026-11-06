//! The admission rule of [`RunStore::create`]: one live run per session.

use super::commit_status;
use super::fixtures::new_run;
use crate::ids::{RunId, SessionId};
use crate::run::RunStatus;
use crate::store::{RunStore, StoreError};

/// Check the admission rule of [`RunStore::create`]: a session holds at
/// most one run that is not terminal, per namespace, and a refused create
/// writes nothing. Panics on the first rule that fails.
pub async fn run_admission_suite(store: &dyn RunStore) {
    let ns = "conformance/admission";
    let on = |run: &str, session: &str| {
        let mut new = new_run(ns, run, 1);
        new.session_id = SessionId::new(session);
        new
    };
    store.create(on("r1", "s1")).await.expect("a first run");
    let busy = |result: Result<_, StoreError>| match result {
        Err(StoreError::SessionBusy { session_id, run_id }) => {
            assert_eq!((session_id.as_str(), run_id.as_str()), ("s1", "r1"));
        }
        other => panic!("a second live run on a session is SessionBusy: {other:?}"),
    };
    busy(store.create(on("r2", "s1")).await);
    assert!(
        matches!(
            store.load(ns, &RunId::new("r2")).await,
            Err(StoreError::NotFound { .. })
        ),
        "a refused create writes nothing"
    );
    store
        .create(on("r3", "s2"))
        .await
        .expect("another session is free");
    let mut elsewhere = on("r2", "s1");
    elsewhere.namespace = "conformance/admission-other".into();
    store
        .create(elsewhere)
        .await
        .expect("another namespace is free");

    // Running and suspended runs hold the session too; a terminal one does not.
    let lease = store.claim(ns, &RunId::new("r1"), "w").await.unwrap();
    busy(store.create(on("r2", "s1")).await);
    commit_status(store, &lease, RunStatus::AwaitingApproval).await;
    busy(store.create(on("r2", "s1")).await);
    let lease = store.claim(ns, &RunId::new("r1"), "w").await.unwrap();
    commit_status(store, &lease, RunStatus::Completed).await;
    store
        .create(on("r2", "s1"))
        .await
        .expect("the session is free again");
}
