//! Dependency edges: reciprocal, idempotent, kept for future tasks, and
//! removed with the task they name.

use super::{On, status};
use crate::store::{SessionStore, TaskPatch, TaskStatus, TaskStore};

pub(super) async fn check<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    for from_blocker in [true, false] {
        edges_are_reciprocal(store, from_blocker).await;
        edges_to_future_tasks_are_kept(store, from_blocker).await;
        an_add_wins_over_a_removal(store, from_blocker).await;
    }
    deletes_remove_edges(store).await;
}

fn ids(ids: &[&str]) -> Vec<String> {
    ids.iter().map(|id| (*id).to_owned()).collect()
}

/// A patch adding (or with `remove`, removing) the edge "1 blocks 2", made
/// on task 1 when `from_blocker`, else on task 2; and that task's id.
fn edge(from_blocker: bool, remove: bool) -> (&'static str, TaskPatch) {
    let mut patch = TaskPatch::default();
    let (id, list, other) = match (from_blocker, remove) {
        (true, false) => ("1", &mut patch.add_blocks, "2"),
        (true, true) => ("1", &mut patch.remove_blocks, "2"),
        (false, false) => ("2", &mut patch.add_blocked_by, "1"),
        (false, true) => ("2", &mut patch.remove_blocked_by, "1"),
    };
    // Twice: adding or removing an edge again changes nothing.
    *list = ids(&[other, other]);
    (id, patch)
}

async fn blocker_and_blocked<'a, S: SessionStore + TaskStore + ?Sized>(
    store: &'a S,
    name: String,
) -> On<'a, S> {
    let on = On::new(store, &name).await;
    on.create("blocker").await;
    on.create("blocked").await;
    on
}

/// "1 blocks 2", added from either end, shows on both.
async fn edges_are_reciprocal<S: SessionStore + TaskStore + ?Sized>(store: &S, from_blocker: bool) {
    let on = blocker_and_blocked(store, format!("edges-{from_blocker}")).await;
    let (id, add) = edge(from_blocker, false);
    on.patch(id, add.clone()).await.unwrap();
    on.patch(id, add).await.unwrap();
    assert_eq!(on.get("1").await.blocks, ["2"], "added on {id}");
    assert_eq!(on.get("2").await.blocked_by, ["1"], "added on {id}");
    assert!(on.get("1").await.blocked_by.is_empty() && on.get("2").await.blocks.is_empty());
    assert_eq!(on.ready().await, ["1"], "a blocked task is not ready");
    on.patch("1", status(TaskStatus::Completed)).await.unwrap();
    assert_eq!(on.ready().await, ["2"], "ready once its blocker completes");

    let (id, remove) = edge(from_blocker, true);
    on.patch(id, remove.clone()).await.unwrap();
    on.patch(id, remove).await.unwrap();
    assert!(on.get("1").await.blocks.is_empty(), "removed on {id}");
    assert!(on.get("2").await.blocked_by.is_empty(), "removed on {id}");
}

/// An edge to an id not handed out yet shows up on that task once created.
async fn edges_to_future_tasks_are_kept<S: SessionStore + TaskStore + ?Sized>(
    store: &S,
    blocks_future: bool,
) {
    let on = On::new(store, &format!("future-{blocks_future}")).await;
    on.create("first").await;
    let mut future = TaskPatch::default();
    match blocks_future {
        true => future.add_blocks = ids(&["2"]),
        false => future.add_blocked_by = ids(&["2"]),
    }
    on.patch("1", future).await.unwrap();
    on.create("second").await;
    let (blocker, blocked) = if blocks_future {
        ("1", "2")
    } else {
        ("2", "1")
    };
    assert_eq!(on.get(blocker).await.blocks, [blocked]);
    assert_eq!(on.get(blocked).await.blocked_by, [blocker]);
    assert_eq!(on.ready().await, [blocker]);
}

/// Removals apply first, so one patch removing and adding an edge keeps it.
async fn an_add_wins_over_a_removal<S: SessionStore + TaskStore + ?Sized>(
    store: &S,
    from_blocker: bool,
) {
    let on = blocker_and_blocked(store, format!("add-wins-{from_blocker}")).await;
    let (id, mut both) = edge(from_blocker, false);
    let (_, remove) = edge(from_blocker, true);
    both.remove_blocks = remove.remove_blocks;
    both.remove_blocked_by = remove.remove_blocked_by;
    on.patch(id, both).await.unwrap();
    assert_eq!(on.get("1").await.blocks, ["2"], "kept on {id}");
    assert_eq!(on.get("2").await.blocked_by, ["1"], "kept on {id}");
}

async fn deletes_remove_edges<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    let on = On::new(store, "delete").await;
    for subject in ["a", "b", "c"] {
        on.create(subject).await;
    }
    let edges = TaskPatch {
        add_blocks: ids(&["3", "2", "5"]),
        ..TaskPatch::default()
    };
    on.patch("1", edges).await.unwrap();
    assert_eq!(
        on.get("1").await.blocks,
        ["3", "2", "5"],
        "edges in the order added"
    );
    assert!(!on.delete("5").await, "there is no task 5");
    assert_eq!(
        on.get("1").await.blocks,
        ["3", "2", "5"],
        "so nothing changed"
    );
    assert!(on.delete("1").await);
    assert_eq!(on.find("1").await, None, "deleted");
    assert!(
        on.get("2").await.blocked_by.is_empty(),
        "its edges went with it"
    );
    assert_eq!(on.ready().await, ["2", "3"]);
    assert!(!on.delete("1").await, "a second delete finds nothing");

    let missing = TaskPatch {
        add_blocked_by: ids(&["77"]),
        ..TaskPatch::default()
    };
    on.patch("3", missing).await.unwrap();
    assert_eq!(
        on.ready().await,
        ["2"],
        "a missing blocker is not completed"
    );
}
