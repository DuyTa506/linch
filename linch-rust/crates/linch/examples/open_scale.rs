//! Times `AgentConfig::open` against a Postgres schema: how long N concurrent
//! opens take, in one process and (run it several times) across processes.
//!
//! ```text
//! LINCH_TEST_PG_DSN=postgres://... open_scale prep   <schema>
//! LINCH_TEST_PG_DSN=postgres://... open_scale own    <schema> <n> [start_at_ms]
//! LINCH_TEST_PG_DSN=postgres://... open_scale shared <schema> <n> [start_at_ms]
//! LINCH_TEST_PG_DSN=postgres://... open_scale fresh  <prefix> <n> [start_at_ms]
//! LINCH_TEST_PG_DSN=postgres://... open_scale drop   <schema>
//! ```
//!
//! `own`: every open has a `PgPools` of its own (so its own pool and its own
//! migrate call), as N agents in N processes would. `shared`: one `PgPools`
//! for all N, as a server's agents have. `fresh`: every open has a schema of
//! its own that still needs its migrations. Prints one line: the opens'
//! latencies in milliseconds (p50, p90, max) and the wall time.

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use linch::{AgentConfig, Hooks, OpenedAgent, PgPools, ToolRegistry};
use serde_json::json;
use sqlx::postgres::{PgConnection, PgPoolOptions};
use sqlx::{AssertSqlSafe, Connection, PgPool};

fn dsn_for(base: &str, schema: &str) -> String {
    let separator = if base.contains('?') { '&' } else { '?' };
    format!("{base}{separator}options=-c%20search_path%3D{schema}")
}

async fn run(admin: &PgPool, sql: String) {
    let query = sqlx::query(AssertSqlSafe(sql));
    query.execute(admin).await.expect("sql");
}

async fn open(dsn: &str, pools: &PgPools) -> OpenedAgent {
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "scale",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": []},
        "stores": {"type": "postgres", "dsn": dsn},
    }))
    .expect("config");
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    config.open_with(tools, hooks, pools).await.expect("open")
}

async fn wait_until(start_at_ms: Option<u64>) {
    let Some(at) = start_at_ms else { return };
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    let now = u64::try_from(now.as_millis()).unwrap();
    tokio::time::sleep(Duration::from_millis(at.saturating_sub(now))).await;
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let base = std::env::var("LINCH_TEST_PG_DSN").expect("LINCH_TEST_PG_DSN");
    let (mode, name) = (args[1].as_str(), args[2].as_str());
    let n: usize = args.get(3).map_or(1, |n| n.parse().unwrap());
    let start_at: Option<u64> = args.get(4).map(|t| t.parse().unwrap());
    let admin = PgPoolOptions::new()
        .max_connections(1)
        .connect(&base)
        .await
        .expect("connect");
    match mode {
        "prep" => {
            run(&admin, format!("DROP SCHEMA IF EXISTS {name} CASCADE")).await;
            run(&admin, format!("CREATE SCHEMA {name}")).await;
            let pools = PgPools::new();
            open(&dsn_for(&base, name), &pools).await.close().await;
        }
        // Backends of one open that a count of `pg_stat_activity` sees right
        // after `open` returns and a moment later: the pool's connection
        // stays, the connection that held the migration lock only lingers.
        "linger" => {
            let app = format!("linger_{}", std::process::id());
            let dsn = format!("{}&application_name={app}", dsn_for(&base, name));
            let pools = PgPools::new();
            let agent = open(&dsn, &pools).await;
            let count = || async {
                let sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1";
                sqlx::query_scalar::<_, i64>(sql)
                    .bind(&app)
                    .fetch_one(&admin)
                    .await
                    .expect("count")
            };
            let mut seen = vec![count().await];
            for pause in [5, 20, 100] {
                tokio::time::sleep(Duration::from_millis(pause)).await;
                seen.push(count().await);
            }
            println!("backends right after open, then +5ms, +25ms, +125ms: {seen:?}");
            agent.close().await;
        }
        "drop" => run(&admin, format!("DROP SCHEMA IF EXISTS {name} CASCADE")).await,
        // Where an open's time goes on a migrated schema: the pool's first
        // connection, a bare second connection (what `migrate` takes for its
        // lock), the lock statement itself, and the whole `migrate` call.
        "phases" => {
            let dsn = dsn_for(&base, name);
            let max = linch_postgres::default_max_connections();
            let started = Instant::now();
            let pool = linch_postgres::connect(&dsn, linch_postgres::CONNECT_TIMEOUT, max).await;
            let pool = pool.expect("connect");
            let pool_connect = started.elapsed();
            let started = Instant::now();
            let mut bare = PgConnection::connect_with(&pool.connect_options())
                .await
                .expect("bare connection");
            let bare_connect = started.elapsed();
            let started = Instant::now();
            sqlx::query("SELECT pg_advisory_lock(42)")
                .execute(&mut bare)
                .await
                .expect("lock");
            let lock = started.elapsed();
            bare.close().await.expect("close");
            let started = Instant::now();
            linch_postgres::migrate(&pool).await.expect("migrate");
            let migrate = started.elapsed();
            println!(
                "pool_connect={pool_connect:?} bare_connect={bare_connect:?} \
                 lock_stmt={lock:?} migrate_total={migrate:?}"
            );
        }
        "own" | "shared" | "fresh" => {
            // One `PgPools` for the whole process, for `shared`.
            let shared: &'static PgPools = Box::leak(Box::new(PgPools::new()));
            let mut schemas = Vec::new();
            for i in 0..n {
                if mode == "fresh" {
                    let schema = format!("{name}_{}_{i}", std::process::id());
                    run(&admin, format!("DROP SCHEMA IF EXISTS {schema} CASCADE")).await;
                    run(&admin, format!("CREATE SCHEMA {schema}")).await;
                    schemas.push(schema);
                } else {
                    schemas.push(name.to_owned());
                }
            }
            wait_until(start_at).await;
            let wall = Instant::now();
            let mut tasks = Vec::new();
            for schema in &schemas {
                let dsn = dsn_for(&base, schema);
                let own = (mode != "shared").then(PgPools::new);
                tasks.push(tokio::spawn(async move {
                    let started = Instant::now();
                    let agent = open(&dsn, own.as_ref().unwrap_or(shared)).await;
                    let took = started.elapsed();
                    agent.close().await;
                    took
                }));
            }
            let mut took = Vec::new();
            for task in tasks {
                took.push(task.await.unwrap());
            }
            let wall = wall.elapsed();
            took.sort();
            let ms = |d: Duration| d.as_secs_f64() * 1000.0;
            let at = |q: f64| ms(took[((took.len() - 1) as f64 * q) as usize]);
            println!(
                "{mode} n={n} p50={:.0}ms p90={:.0}ms max={:.0}ms wall={:.0}ms",
                at(0.5),
                at(0.9),
                ms(*took.last().unwrap()),
                ms(wall)
            );
            if mode == "fresh" {
                for schema in schemas {
                    run(&admin, format!("DROP SCHEMA IF EXISTS {schema} CASCADE")).await;
                }
            }
        }
        other => panic!("unknown mode {other}"),
    }
}
