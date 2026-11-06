//! Creates racing on one session: the unique index admits exactly one, and
//! every other create names the run that got in.

use linch_core::{RunStore, SessionId, StoreError};
use linch_postgres::PgRunStore;
use tokio::task::JoinSet;

use crate::common::{TestDb, new_run};

#[tokio::test]
async fn racing_creates_on_one_session_admit_one() {
    let Some(db) = TestDb::migrated("admission_race").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    for round in 0..10 {
        let session = SessionId::new(format!("s{round}"));
        let mut creates = JoinSet::new();
        for i in 0..4 {
            let (store, mut run) = (store.clone(), new_run("ns", &format!("r{round}-{i}"), 1));
            run.session_id = session.clone();
            creates.spawn(async move { store.create(run).await });
        }
        let (mut admitted, mut busy) = (Vec::new(), Vec::new());
        while let Some(result) = creates.join_next().await {
            match result.unwrap() {
                Ok(record) => admitted.push(record.run_id),
                Err(StoreError::SessionBusy { session_id, run_id }) => {
                    assert_eq!(session_id, session);
                    busy.push(run_id);
                }
                Err(other) => panic!("unexpected {other:?}"),
            }
        }
        assert_eq!(admitted.len(), 1, "round {round}");
        assert!(busy.iter().all(|id| *id == admitted[0]), "{busy:?}");
    }
    db.finish().await;
}
