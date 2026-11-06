//! Session tasks on Postgres: the conformance suite, and writes that race
//! from separate connections and pools.

use linch_core::store::conformance::task_store_suite;
use linch_core::{
    LogEntry, Message, MessageEntry, NewTask, SessionId, SessionStore, TaskPatch, TaskStore,
};
use linch_postgres::PgSessionStore;
use sqlx::postgres::PgPoolOptions;
use tokio::task::JoinSet;

use crate::common::TestDb;

#[tokio::test]
async fn task_store_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("tasks").await else {
        return;
    };
    task_store_suite(&PgSessionStore::new(db.pool.clone())).await;
    db.finish().await;
}

/// Two stores on separate pools, as two processes would be, create tasks in
/// one session at once: every id is handed out exactly once.
#[tokio::test]
async fn two_pools_hand_out_distinct_ids() {
    let Some(db) = TestDb::migrated("tasks_pools").await else {
        return;
    };
    let other = PgPoolOptions::new()
        .max_connections(4)
        .connect_with(db.options.clone())
        .await
        .unwrap();
    let stores = [
        PgSessionStore::new(db.pool.clone()),
        PgSessionStore::new(other.clone()),
    ];
    let sid = SessionId::new("s1");
    stores[0].create("ns", &sid).await.unwrap();
    let mut creates = JoinSet::new();
    for n in 0..20 {
        let (store, sid) = (stores[n % 2].clone(), sid.clone());
        let task = NewTask::new(format!("t{n}"), "");
        creates.spawn(async move { store.create_task("ns", &sid, task).await });
    }
    let mut ids: Vec<u64> = creates
        .join_all()
        .await
        .into_iter()
        .map(|task| task.unwrap().id.parse().unwrap())
        .collect();
    ids.sort_unstable();
    assert_eq!(ids, (1..=20).collect::<Vec<u64>>());
    other.close().await;
    db.finish().await;
}

/// Task 1 and task 2 each add both edges between them, in opposite orders,
/// at once. The session lock makes them take turns: no deadlock, and each
/// edge is stored once.
#[tokio::test]
async fn opposite_edge_writes_do_not_deadlock() {
    let Some(db) = TestDb::migrated("tasks_edges").await else {
        return;
    };
    let store = PgSessionStore::new(db.pool.clone());
    for round in 0..10 {
        let sid = SessionId::new(format!("s{round}"));
        store.create("ns", &sid).await.unwrap();
        for subject in ["a", "b"] {
            store
                .create_task("ns", &sid, NewTask::new(subject, ""))
                .await
                .unwrap();
        }
        let both_ways = |other: &str| TaskPatch {
            add_blocks: vec![other.into()],
            add_blocked_by: vec![other.into()],
            ..TaskPatch::default()
        };
        let (one, two) = tokio::join!(
            store.update_task("ns", &sid, "1", both_ways("2")),
            store.update_task("ns", &sid, "2", both_ways("1")),
        );
        one.unwrap().unwrap();
        two.unwrap().unwrap();
        for (id, other) in [("1", "2"), ("2", "1")] {
            let task = store.get_task("ns", &sid, id).await.unwrap().unwrap();
            assert_eq!(
                (task.blocks, task.blocked_by),
                (vec![other.to_owned()], vec![other.to_owned()])
            );
        }
    }
    db.finish().await;
}

/// Task writes and log appends both lock the session row; racing, both land.
#[tokio::test]
async fn task_writes_and_log_appends_both_land() {
    let Some(db) = TestDb::migrated("tasks_appends").await else {
        return;
    };
    let store = PgSessionStore::new(db.pool.clone());
    let sid = SessionId::new("s1");
    store.create("ns", &sid).await.unwrap();
    let entry = |n: u64| {
        LogEntry::Message(MessageEntry {
            message: Message::user_text(format!("m{n}")).into(),
            visible: true,
            historical: true,
        })
    };
    for n in 0..10 {
        let (appended, created) = tokio::join!(
            store.append("ns", &sid, n, vec![entry(n)]),
            store.create_task("ns", &sid, NewTask::new(format!("t{n}"), "")),
        );
        assert_eq!(appended.unwrap(), n + 1);
        assert_eq!(created.unwrap().id, (n + 1).to_string());
    }
    assert_eq!(store.load("ns", &sid).await.unwrap().len(), 10);
    assert_eq!(store.list_tasks("ns", &sid).await.unwrap().len(), 10);
    db.finish().await;
}
