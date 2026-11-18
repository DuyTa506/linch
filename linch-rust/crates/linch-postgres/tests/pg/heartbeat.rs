//! What a run's heartbeat needs from its process: a connection from the
//! agent's pool and one durable commit, every third of the lease. Two
//! `#[ignore]`d checks: a RED test for the pool, and a probe that times each
//! part of a beat.
//!
//! ```text
//! LINCH_TEST_PG_DSN=... cargo test -p linch-postgres --test pg heartbeat -- --ignored --nocapture
//! ```

use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, PermissionEngine, PermissionMode, PermissionPolicy, Provider,
    RunError, RunId, RunOptions, RunStream, ScriptedProvider, StoreError,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::{Notify, Semaphore};

use crate::common::TestDb;
use crate::idle::Gated;

const MODEL: &str = "scripted-model";
const TTL: Duration = Duration::from_millis(600);

/// An agent on `pool` whose model call waits at the returned gate.
fn agent_on(pool: &PgPool) -> (Arc<Gated>, Agent) {
    let turn = TurnBuilder::new(MODEL).text("answer").build();
    let provider = Arc::new(Gated {
        inner: ScriptedProvider::new([turn]),
        entered: Notify::new(),
        open: Semaphore::new(0),
    });
    let runs = PgRunStore::new(pool.clone()).with_lease_ttl(TTL);
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::clone(&provider) as Arc<dyn Provider>)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(runs))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace("ns")
        .build()
        .unwrap();
    (provider, agent)
}

/// Start a run and poll it until its model call waits at the gate.
async fn at_the_gate(provider: &Gated, agent: &Agent) -> RunStream {
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let polled = async {
        while let Some(item) = stream.next().await {
            item.expect("the run goes on until the gate");
        }
    };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = provider.entered.notified() => {}
    }
    stream
}

async fn pool_of(db: &TestDb, size: u32) -> PgPool {
    let options = db.options.clone();
    let pool = PgPoolOptions::new()
        .max_connections(size)
        .connect_with(options);
    pool.await.expect("connect")
}

/// RED. A's process is healthy, its model call is in flight, and every
/// connection of its pool is busy with run work that waits (a row lock held
/// elsewhere, a slow query, a stalled disk): the beat's `renew` queues for a
/// connection behind that work, the lease runs out, and B takes the run over
/// while A is alive, so the model call is paid twice.
#[tokio::test]
#[ignore = "RED: the heartbeat shares the run work's pool, so a saturated pool starves it"]
async fn a_saturated_pool_does_not_starve_the_heartbeat() {
    let Some(db) = TestDb::migrated("hb_saturated").await else {
        return;
    };
    let pool_a = pool_of(&db, 3).await;
    let (gate_a, a) = agent_on(&pool_a);
    let (gate_b, b) = agent_on(&pool_of(&db, 3).await);
    gate_b.open.add_permits(Semaphore::MAX_PERMITS);
    let mut stream_a = at_the_gate(&gate_a, &a).await;
    let run_id: RunId = stream_a.run_id().clone();

    // Run work that holds all 3 connections for 3 leases.
    let busy: Vec<_> = (0..3)
        .map(|_| {
            let pool = pool_a.clone();
            tokio::spawn(async move {
                sqlx::query("SELECT pg_sleep($1)")
                    .bind((TTL * 3).as_secs_f64())
                    .execute(&pool)
                    .await
            })
        })
        .collect();
    tokio::time::sleep(Duration::from_millis(100)).await;
    // A's stream is polled the whole time, so its heartbeat runs.
    let polled = async {
        while let Some(item) = stream_a.next().await {
            item.expect("the run waits at the gate");
        }
    };
    let _ = tokio::time::timeout(TTL * 2, polled).await;

    let items: Vec<Result<_, RunError>> = b.resume(run_id, RunOptions::new("s1")).collect().await;
    let refused = matches!(
        items.as_slice(),
        [Err(RunError::Store(StoreError::RunLeased { .. }))]
    );
    assert!(refused, "B took over a run whose holder is alive");
    for task in busy {
        let _ = task.await;
    }
    db.finish().await;
}

fn quantiles(mut samples: Vec<f64>) -> String {
    samples.sort_by(f64::total_cmp);
    let at = |q: f64| samples[((samples.len() - 1) as f64 * q) as usize];
    format!(
        "p50={:.1}ms p99={:.1}ms max={:.1}ms",
        at(0.5),
        at(0.99),
        samples[samples.len() - 1]
    )
}

/// Times what one beat does, every third of the lease for `PROBE_SECS`
/// seconds (20 by default): the timer's overshoot (scheduling), the checkout
/// of a pool connection, and the `UPDATE` that renews (a durable commit). Run
/// it beside a loaded disk to see which one is late. A beat is late enough to
/// lose the lease when the three add up past two thirds of the lease.
#[tokio::test]
#[ignore = "diagnostic: prints the timings of a beat"]
async fn heartbeat_latency_probe() {
    let Some(db) = TestDb::migrated("hb_probe").await else {
        return;
    };
    let secs: u64 = std::env::var("PROBE_SECS").map_or(20, |s| s.parse().unwrap());
    let pool = pool_of(&db, 3).await;
    let (gate, agent) = agent_on(&pool);
    let stream = at_the_gate(&gate, &agent).await;
    let run_id = stream.run_id().as_str().to_owned();
    let every = TTL / 3;
    let (mut sleep, mut checkout, mut update) = (vec![], vec![], vec![]);
    let (mut late, mut total) = (0, 0);
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        let started = Instant::now();
        tokio::time::sleep(every).await;
        let overshoot = started.elapsed().saturating_sub(every);
        let acquiring = Instant::now();
        let mut connection = pool.acquire().await.unwrap();
        let acquired = acquiring.elapsed();
        let updating = Instant::now();
        sqlx::query(
            "UPDATE linch_runs SET lease_until = clock_timestamp() + $2::float8 * interval '1 millisecond' \
             WHERE run_id = $1",
        )
        .bind(&run_id)
        .bind(TTL.as_secs_f64() * 1000.0)
        .execute(&mut *connection)
        .await
        .unwrap();
        let updated = updating.elapsed();
        drop(connection);
        let ms = |d: Duration| d.as_secs_f64() * 1000.0;
        sleep.push(ms(overshoot));
        checkout.push(ms(acquired));
        update.push(ms(updated));
        total += 1;
        if overshoot + acquired + updated > TTL - every {
            late += 1;
        }
    }
    println!("beats={total} lease={TTL:?} every={every:?} beats that would lose the lease={late}");
    println!("timer overshoot  {}", quantiles(sleep));
    println!("pool checkout    {}", quantiles(checkout));
    println!("UPDATE (commit)  {}", quantiles(update));
    drop(stream);
    db.finish().await;
}
