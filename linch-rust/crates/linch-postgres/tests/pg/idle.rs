//! A connection the server closed while it sat in the pool is replaced
//! before the next write, so the receipt write after a paid model call
//! lands and a resume never pays for that call again.

use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::events::kinds::ResultSubtype;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, PermissionEngine, PermissionMode, PermissionPolicy, Provider,
    ProviderError, ProviderRequest, ProviderStream, RunOptions, ScriptedProvider,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;

use crate::common::TestDb;

const MODEL: &str = "scripted-model";

/// A scripted provider whose calls wait until the test opens the gate.
struct Gated {
    inner: ScriptedProvider,
    /// Notified when a call reaches the gate.
    entered: Notify,
    /// One permit per call let through.
    open: Semaphore,
}

#[async_trait]
impl Provider for Gated {
    fn id(&self) -> &str {
        "gated"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.entered.notify_one();
        self.open.acquire().await.expect("never closed").forget();
        self.inner.stream(request, cancel).await
    }
}

/// Terminate every backend of `application`, only this test's pool, and
/// wait until they are gone.
async fn terminate(admin: &PgPool, application: &str) {
    let killed: Vec<bool> = sqlx::query_scalar(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE application_name = $1 AND pid <> pg_backend_pid()",
    )
    .bind(application)
    .fetch_all(admin)
    .await
    .expect("terminate the pool's backends");
    assert!(!killed.is_empty(), "the pool holds connections");
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let left: i64 =
            sqlx::query_scalar("SELECT count(*) FROM pg_stat_activity WHERE application_name = $1")
                .bind(application)
                .fetch_one(admin)
                .await
                .expect("count the pool's backends");
        if left == 0 {
            return;
        }
        assert!(Instant::now() < deadline, "the backends did not exit");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

#[tokio::test]
async fn a_run_survives_the_server_closing_its_idle_connections() {
    let Some(db) = TestDb::migrated("idle_closed").await else {
        return;
    };
    let dsn = std::env::var("LINCH_TEST_PG_DSN").expect("TestDb read it");
    let application = format!("linch_idle_closed_{}", std::process::id());
    let separator = if dsn.contains('?') { '&' } else { '?' };
    let dsn = format!(
        "{dsn}{separator}application_name={application}&options=-c%20search_path%3D{}",
        db.schema
    );
    let two = NonZeroU32::new(2).unwrap();
    let pool = linch_postgres::connect(&dsn, linch_postgres::CONNECT_TIMEOUT, two)
        .await
        .expect("connect");
    let provider = Arc::new(Gated {
        inner: ScriptedProvider::new([TurnBuilder::new(MODEL).text("done").build()]),
        entered: Notify::new(),
        open: Semaphore::new(0),
    });
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider.clone())
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(PgRunStore::new(pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace("ns")
        .build()
        .unwrap();

    let stream = agent.run("hi", RunOptions::new("s1"));
    let drain = stream.collect::<Vec<_>>();
    let server_restart = async {
        provider.entered.notified().await;
        // The model call is in flight and the pool's connections sit idle:
        // the server closes them, and the call runs on past the threshold
        // after which `connect`'s pool checks a connection it hands out.
        terminate(&db.admin, &application).await;
        let margin = Duration::from_millis(500);
        tokio::time::sleep(linch_postgres::IDLE_PING_AFTER + margin).await;
        provider.open.add_permits(1);
    };
    let (items, ()) = tokio::join!(drain, server_restart);

    let events: Vec<Event> = (items.into_iter())
        .map(|item| item.expect("the first stream completes").event)
        .collect();
    let subtype = events.iter().find_map(|event| match event {
        Event::Result(result) => Some(result.subtype),
        _ => None,
    });
    assert_eq!(subtype, Some(ResultSubtype::Success));
    assert_eq!(provider.inner.requests().len(), 1, "one model call");
    pool.close().await;
    db.finish().await;
}
