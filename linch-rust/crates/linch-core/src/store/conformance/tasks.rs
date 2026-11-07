//! [`TaskStore`] rules: ids, field patches, dependency edges and claims.

mod edges;

use futures::future::join_all;
use serde_json::json;

use crate::ids::SessionId;
use crate::json::{JsonObject, JsonValue};
use crate::store::{NewTask, SessionStore, StoreError, Task, TaskPatch, TaskStatus, TaskStore};

/// Check every [`TaskStore`] rule on a store that also holds the sessions.
/// Panics on the first rule that fails.
pub async fn task_store_suite<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    missing_sessions_have_no_tasks(store).await;
    ids_count_up_per_session(store).await;
    patches_replace_fields_and_merge_metadata(store).await;
    edges::check(store).await;
    claims_are_exclusive(store).await;
    concurrent_writes_serialize(store).await;
}

/// Session `s1` of a namespace of its own, with short calls on it.
struct On<'a, S: ?Sized> {
    store: &'a S,
    ns: String,
    sid: SessionId,
}

impl<'a, S: SessionStore + TaskStore + ?Sized> On<'a, S> {
    async fn new(store: &'a S, ns: &str) -> Self {
        let sid = SessionId::new("s1");
        let ns = format!("conformance/tasks-{ns}");
        store.create(&ns, &sid).await.expect("create a session");
        Self { store, ns, sid }
    }

    async fn create(&self, subject: &str) -> Task {
        let task = NewTask::new(subject, "");
        let created = self.store.create_task(&self.ns, &self.sid, task).await;
        created.expect("create a task")
    }

    async fn find(&self, id: &str) -> Option<Task> {
        self.store.get_task(&self.ns, &self.sid, id).await.unwrap()
    }

    async fn get(&self, id: &str) -> Task {
        self.find(id)
            .await
            .unwrap_or_else(|| panic!("task {id} exists"))
    }

    async fn patch(&self, id: &str, patch: TaskPatch) -> Option<Task> {
        let updated = self.store.update_task(&self.ns, &self.sid, id, patch);
        updated.await.unwrap()
    }

    async fn delete(&self, id: &str) -> bool {
        self.store
            .delete_task(&self.ns, &self.sid, id)
            .await
            .unwrap()
    }

    async fn claim(&self, id: &str, owner: &str) -> Option<Task> {
        let claimed = self.store.claim_task(&self.ns, &self.sid, id, owner);
        claimed.await.unwrap()
    }

    async fn release(&self, id: &str) -> Option<Task> {
        let released = self.store.release_task(&self.ns, &self.sid, id);
        released.await.unwrap()
    }

    async fn ready(&self) -> Vec<String> {
        let tasks = self.store.ready_tasks(&self.ns, &self.sid).await.unwrap();
        tasks.into_iter().map(|task| task.id).collect()
    }
}

fn object(value: JsonValue) -> JsonObject {
    match value {
        JsonValue::Object(object) => object,
        _ => unreachable!("an object literal"),
    }
}

fn status(status: TaskStatus) -> TaskPatch {
    TaskPatch {
        status: Some(status),
        ..TaskPatch::default()
    }
}

async fn missing_sessions_have_no_tasks<S: TaskStore + ?Sized>(store: &S) {
    let (ns, sid) = ("conformance/tasks-missing", &SessionId::new("nobody"));
    assert!(
        matches!(
            store.create_task(ns, sid, NewTask::new("a", "b")).await,
            Err(StoreError::NotFound { .. })
        ),
        "create_task in a missing session is NotFound"
    );
    let done = status(TaskStatus::Completed);
    assert_eq!(store.get_task(ns, sid, "1").await.unwrap(), None);
    assert!(store.list_tasks(ns, sid).await.unwrap().is_empty());
    assert!(store.ready_tasks(ns, sid).await.unwrap().is_empty());
    assert_eq!(store.update_task(ns, sid, "1", done).await.unwrap(), None);
    assert!(!store.delete_task(ns, sid, "1").await.unwrap());
    assert_eq!(store.claim_task(ns, sid, "1", "w").await.unwrap(), None);
    assert_eq!(store.release_task(ns, sid, "1").await.unwrap(), None);
}

async fn ids_count_up_per_session<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    let on = On::new(store, "ids").await;
    let mut new = NewTask::new("first", "do it");
    new.active_form = Some("Doing it".into());
    new.metadata = object(json!({"k": [1, {"b": 2, "a": 1}]}));
    let first = store.create_task(&on.ns, &on.sid, new.clone()).await;
    let first = first.unwrap();
    assert_eq!(first.id, "1", "ids start at 1");
    assert_eq!(first.session_id, on.sid);
    assert_eq!(
        (first.subject.as_str(), first.description.as_str()),
        ("first", "do it")
    );
    assert_eq!(first.active_form, new.active_form);
    assert_eq!(first.metadata, new.metadata, "metadata kept as given");
    assert_eq!(
        (first.status, first.owner.as_deref()),
        (TaskStatus::Pending, None)
    );
    assert!(first.blocks.is_empty() && first.blocked_by.is_empty());
    assert_eq!(first.created_at_ms, first.updated_at_ms);
    assert_eq!(on.get("1").await, first, "get returns what create did");
    for n in 2..=11 {
        on.create(&format!("t{n}")).await;
    }
    let all = store.list_tasks(&on.ns, &on.sid).await.unwrap();
    let listed: Vec<&str> = all.iter().map(|task| task.id.as_str()).collect();
    let expected: Vec<String> = (1..=11).map(|n| n.to_string()).collect();
    assert_eq!(listed, expected, "listed in numeric id order");
    for odd in ["01", "abc", "", "-1", "1.0"] {
        assert_eq!(on.find(odd).await, None, "no task {odd:?}");
    }
    assert!(on.delete("11").await);
    assert_eq!(on.create("next").await.id, "12", "ids are not reused");

    let other = SessionId::new("s2");
    store.create(&on.ns, &other).await.unwrap();
    let other = store
        .create_task(&on.ns, &other, NewTask::new("a", ""))
        .await;
    assert_eq!(other.unwrap().id, "1", "ids are per session");
    let elsewhere = On::new(store, "ids-elsewhere").await;
    assert_eq!(elsewhere.create("a").await.id, "1", "and per namespace");
}

async fn patches_replace_fields_and_merge_metadata<S: SessionStore + TaskStore + ?Sized>(
    store: &S,
) {
    let on = On::new(store, "fields").await;
    let mut new = NewTask::new("old", "old text");
    new.metadata = object(json!({"keep": 1, "drop": 2}));
    let created = store.create_task(&on.ns, &on.sid, new).await.unwrap();
    let full = TaskPatch {
        subject: Some("new".into()),
        description: Some(String::new()),
        active_form: Some("Working".into()),
        owner: Some("w1".into()),
        metadata: object(json!({"drop": null, "add": [1]})),
        ..status(TaskStatus::InProgress)
    };
    let updated = on.patch("1", full).await.expect("task 1");
    assert_eq!(
        (updated.subject.as_str(), updated.description.as_str()),
        ("new", "")
    );
    assert_eq!(updated.active_form.as_deref(), Some("Working"));
    assert_eq!(updated.status, TaskStatus::InProgress);
    assert_eq!(updated.owner.as_deref(), Some("w1"));
    let merged = object(json!({"keep": 1, "add": [1]}));
    assert_eq!(updated.metadata, merged, "merged by key, null removes");
    assert_eq!(updated.created_at_ms, created.created_at_ms);
    assert!(updated.updated_at_ms >= created.updated_at_ms);
    assert_eq!(on.get("1").await, updated, "the patch is stored");

    let same = on.patch("1", TaskPatch::default()).await.unwrap();
    assert_eq!(
        (same.subject, same.owner, same.metadata),
        (updated.subject, updated.owner, updated.metadata),
        "an empty patch changes no field; no owner keeps the owner"
    );
    let done = status(TaskStatus::Completed);
    assert_eq!(on.patch("99", done).await, None, "a missing task");
}

async fn claims_are_exclusive<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    let on = On::new(store, "claim").await;
    on.create("a").await;
    let claimed = on.claim("1", "w1").await.expect("claimed");
    assert_eq!(claimed.owner.as_deref(), Some("w1"));
    assert_eq!(
        claimed.status,
        TaskStatus::Pending,
        "a claim leaves the status"
    );
    assert_eq!(on.claim("1", "w2").await, None, "already owned");
    assert_eq!(on.get("1").await.owner.as_deref(), Some("w1"));
    assert!(on.ready().await.is_empty(), "an owned task is not ready");
    assert_eq!(on.claim("999", "w1").await, None, "a missing task");

    on.patch("1", status(TaskStatus::InProgress)).await.unwrap();
    let released = on.release("1").await.expect("task 1");
    assert_eq!(released.owner, None);
    assert_eq!(released.status, TaskStatus::Pending, "back to pending");
    assert_eq!(on.get("1").await, released);
    assert_eq!(on.ready().await, ["1"]);

    on.patch("1", status(TaskStatus::InProgress)).await.unwrap();
    assert_eq!(on.claim("1", "w3").await, None, "not pending");
    let done = TaskPatch {
        owner: Some("w4".into()),
        ..status(TaskStatus::Completed)
    };
    let done = on.patch("1", done).await.unwrap();
    assert_eq!(
        on.release("1").await,
        Some(done),
        "a completed task is kept"
    );
    assert_eq!(on.release("9").await, None);
}

async fn concurrent_writes_serialize<S: SessionStore + TaskStore + ?Sized>(store: &S) {
    let on = On::new(store, "concurrent").await;
    let subjects: Vec<String> = (0..8).map(|n| format!("t{n}")).collect();
    let creates = subjects.iter().map(|subject| on.create(subject));
    let mut created: Vec<u64> = join_all(creates)
        .await
        .into_iter()
        .map(|task| task.id.parse().unwrap())
        .collect();
    created.sort_unstable();
    assert_eq!(
        created,
        (1..=8).collect::<Vec<u64>>(),
        "distinct ids, no gaps"
    );

    let owners: Vec<String> = (0..8).map(|n| format!("w{n}")).collect();
    let claims = owners.iter().map(|owner| on.claim("1", owner));
    let won = join_all(claims).await.into_iter().flatten().count();
    assert_eq!(won, 1, "one of the concurrent claims wins");

    let merges = (0..8).map(|n| {
        let metadata = object(json!({ format!("k{n}"): n }));
        on.patch(
            "2",
            TaskPatch {
                metadata,
                ..TaskPatch::default()
            },
        )
    });
    for merged in join_all(merges).await {
        merged.expect("task 2");
    }
    assert_eq!(on.get("2").await.metadata.len(), 8, "no merge is lost");
}
