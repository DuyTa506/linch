//! Two start jobs on one session take turns (one live run per session),
//! and a job that fails for good fails its run too.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Commit, Envelope, Event, Lease, LogEntry, Message, NewRun,
    PermissionEngine, PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent,
    ProviderRequest, ProviderStream, Role, RunId, RunOptions, RunRecord, RunStatus, RunStore,
    SessionId, SessionStore, StoreError, ToolScope,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use linch_worker::{enqueue_approve, enqueue_run};
use tokio::sync::{Barrier, Mutex};
use tokio_util::sync::CancellationToken;

use crate::common::*;

/// Answers every request with `re: <last user text>`.
struct Reply;

#[async_trait]
impl Provider for Reply {
    fn id(&self) -> &str {
        "reply"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let last = (request.messages.iter().rev())
            .find(|message| message.role == Role::User)
            .map(Message::text)
            .unwrap_or_default();
        let turn = TurnBuilder::new(MODEL).text(format!("re: {last}")).build();
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

/// Holds the first two session loads until both have read, so two runs
/// see the session at the same length before either appends its prompt.
struct Gate {
    inner: PgSessionStore,
    loads: AtomicUsize,
    both: Barrier,
}

#[async_trait]
impl SessionStore for Gate {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        self.inner.create(ns, session).await
    }

    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        let entries = self.inner.load(ns, session).await;
        if self.loads.fetch_add(1, Ordering::SeqCst) < 2 {
            self.both.wait().await;
        }
        entries
    }

    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.inner.append(ns, session, expected, entries).await
    }
}

/// Postgres runs with the admission rule of `RunStore::create`, which
/// `linch-postgres` does not enforce yet: a create on a session with a run
/// that is not terminal is `SessionBusy`. A lock makes the check and the
/// insert one step within this process.
struct Admit {
    inner: PgRunStore,
    pool: PgPool,
    one: Mutex<()>,
}

#[async_trait]
impl RunStore for Admit {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        let _one = self.one.lock().await;
        let active: Option<String> = sqlx::query_scalar(
            "SELECT run_id FROM linch_runs WHERE namespace = $1 AND session_id = $2 \
             AND status NOT IN ('completed', 'failed', 'aborted') LIMIT 1",
        )
        .bind(&run.namespace)
        .bind(run.session_id.as_str())
        .fetch_optional(&self.pool)
        .await
        .expect("the admission query");
        if let Some(run_id) = active {
            let run_id = RunId::new(run_id);
            let session_id = run.session_id;
            return Err(StoreError::SessionBusy { session_id, run_id });
        }
        self.inner.create(run).await
    }

    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.inner.claim(ns, run_id, owner).await
    }

    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        self.inner.load(ns, run_id).await
    }

    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.inner.commit(lease, commit).await
    }

    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.inner.events(ns, run_id, after_seq).await
    }
}

fn reply_agent(pool: &PgPool, runs: Arc<Admit>) -> Agent {
    let gate = Gate {
        inner: PgSessionStore::new(pool.clone()),
        loads: AtomicUsize::new(0),
        both: Barrier::new(2),
    };
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::new(Reply))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(runs)
        .session_store(Arc::new(gate))
        .namespace(NS)
        .build()
        .unwrap()
}

async fn texts(pool: &PgPool, session: &str) -> Vec<String> {
    let sessions = PgSessionStore::new(pool.clone());
    let entries = sessions.load(NS, &SessionId::new(session)).await.unwrap();
    let texts = entries.into_iter().map(|entry| match entry {
        LogEntry::Message(entry) => entry.message.text(),
        LogEntry::Projection(_) => panic!("no compaction here"),
    });
    texts.collect()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_start_jobs_on_one_session_take_turns() {
    let Some(db) = TestDb::new("two_runs").await else {
        return;
    };
    let runs = Arc::new(Admit {
        inner: PgRunStore::new(db.pool.clone()),
        pool: db.pool.clone(),
        one: Mutex::new(()),
    });
    let agent = reply_agent(&db.pool, runs);
    let worker = Running::spawn(&db.pool, config("w1"), agent);
    let s1 = SessionId::new("s1");
    for prompt in ["from a", "from b"] {
        let job = enqueue_run(&db.pool, "default", NS, &s1, prompt).await;
        job.unwrap();
    }
    wait_until("both runs complete and their jobs end", || async {
        let runs = db.runs_of("s1").await;
        let mut done = runs.len() == 2 && db.jobs().await == 0;
        for run in &runs {
            done &= db.status(run).await == RunStatus::Completed;
        }
        done
    })
    .await;
    // Both jobs loaded the session together. The later create was refused
    // (its job started a new run on retry), or came after the first run
    // ended and read the session again: either way each run was created
    // and claimed once, and the two conversations did not interleave.
    for run in db.runs_of("s1").await {
        assert_eq!(db.record(&run).await.epoch, 1, "each run claimed once");
    }
    let log = texts(&db.pool, "s1").await;
    let a_first = ["from a", "re: from a", "from b", "re: from b"];
    let b_first = ["from b", "re: from b", "from a", "re: from a"];
    assert!(log == a_first || log == b_first, "{log:?}");
    worker.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_job_that_fails_for_good_fails_its_run() {
    let Some(db) = TestDb::new("fail_run").await else {
        return;
    };
    // A run of another contract (another tool), suspended for approval.
    let other = agent(&db.pool, Probe::new("Edit", ToolScope::Write, Hold::None));
    let mut stream = other.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    assert_eq!(db.status(&run_id).await, RunStatus::AwaitingApproval);

    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    enqueue_approve(&db.pool, "default", NS, &run_id, approvals)
        .await
        .unwrap();
    wait_until("the job fails for good", || async {
        let failed: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM linch_worker_jobs WHERE failed_at IS NOT NULL",
        )
        .fetch_one(&db.pool)
        .await
        .unwrap();
        failed == 1
    })
    .await;
    // The run failed first, with the job's reason, and no tool ran.
    assert_eq!(db.status(&run_id).await, RunStatus::Failed);
    let events: Vec<Event> = (db.events(&run_id).await.into_iter())
        .map(|envelope| envelope.event)
        .collect();
    let [.., Event::Error(error), Event::Result(_)] = events.as_slice() else {
        panic!("the run must end with an error and a result: {events:?}");
    };
    let message = error.error["message"].as_str().unwrap();
    assert!(message.contains("contract"), "{message}");
    assert_eq!(probe.started(), 0);
    worker.stop().await;
    db.finish().await;
}
