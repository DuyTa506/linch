//! `linch-server --migrate` and `--check`: the schema as a deploy step.
//! Skipped without `LINCH_TEST_PG_DSN`, except the usage errors.

mod common;

use common::pg::TestDb;
use serde_json::Value;
use sqlx::AssertSqlSafe;
use sqlx::postgres::PgPoolOptions;
use tokio::process::Command;

struct Done {
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

async fn run(args: &[&str], dsn: Option<&str>) -> Done {
    let mut command = Command::new(env!("CARGO_BIN_EXE_linch-server"));
    command
        .args(args)
        .env_remove("LINCH_LOG")
        .env_remove("RUST_LOG")
        .env_remove("LINCH_TEST_SCHEMA_DSN")
        .kill_on_drop(true);
    if let Some(dsn) = dsn {
        command.env("LINCH_TEST_SCHEMA_DSN", dsn);
    }
    let output = command.output().await.unwrap();
    Done {
        code: output.status.code(),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }
}

const SOURCE: [&str; 2] = ["--dsn-env", "LINCH_TEST_SCHEMA_DSN"];

fn status(done: &Done) -> Value {
    serde_json::from_str(done.stdout.trim()).unwrap_or_else(|e| panic!("{e}: {}", done.stdout))
}

#[tokio::test]
async fn check_reports_and_migrate_applies_and_both_say_whether_it_is_current() {
    let Some(db) = TestDb::empty("schema_cli").await else {
        return;
    };
    let behind = run(&["--check", SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
    assert_eq!(behind.code, Some(4), "{}", behind.stderr);
    let report = status(&behind);
    assert_eq!(report["current"], false);
    assert_eq!(report["store"]["applied"], Value::Null);
    let latest = report["store"]["latest"].as_i64().unwrap();
    assert!(latest >= 10);

    let migrated = run(&["--migrate", SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
    assert_eq!(migrated.code, Some(0), "{}", migrated.stderr);
    let report = status(&migrated);
    assert_eq!(report["current"], true);
    assert_eq!(report["store"]["applied"], latest);
    assert_eq!(report["worker"]["applied"], report["worker"]["latest"]);

    let current = run(&["--check", SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
    assert_eq!(current.code, Some(0), "{}", current.stderr);
    assert_eq!(status(&current)["current"], true);
    db.drop().await;
}

#[tokio::test]
async fn a_database_a_newer_release_migrated_is_refused_with_its_own_exit_code() {
    let Some(db) = TestDb::empty("schema_cli_newer").await else {
        return;
    };
    let migrated = run(&["--migrate", SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
    assert_eq!(migrated.code, Some(0), "{}", migrated.stderr);
    let latest = status(&migrated)["store"]["latest"].as_i64().unwrap();
    let admin = PgPoolOptions::new().connect(&db.base).await.unwrap();
    let sql = format!(
        "INSERT INTO p11d_schema_cli_newer.linch_migrations \
         (version, description, success, checksum, execution_time) \
         VALUES ({}, 'a later release', true, '\\x00', 0)",
        latest + 2
    );
    sqlx::query(AssertSqlSafe(sql))
        .execute(&admin)
        .await
        .unwrap();

    for flag in ["--check", "--migrate"] {
        let refused = run(&[flag, SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
        assert_eq!(refused.code, Some(3), "{flag}: {}", refused.stderr);
        assert!(refused.stderr.contains("newer"), "{}", refused.stderr);
        assert!(
            !refused.stderr.contains("postgres://"),
            "{}",
            refused.stderr
        );
    }
    db.drop().await;
}

#[tokio::test]
async fn a_missing_variable_or_a_bad_command_line_is_a_usage_error() {
    let unset = run(&["--check", SOURCE[0], SOURCE[1]], None).await;
    assert_eq!(unset.code, Some(2));
    assert!(
        unset.stderr.contains("LINCH_TEST_SCHEMA_DSN"),
        "{}",
        unset.stderr
    );
    let bare = run(&["--migrate"], None).await;
    assert_eq!(bare.code, Some(2));
    assert!(bare.stderr.contains("usage"), "{}", bare.stderr);
}

#[tokio::test]
async fn a_worker_on_a_database_a_newer_release_migrated_refuses_to_start() {
    let Some(db) = TestDb::empty("schema_cli_worker").await else {
        return;
    };
    let migrated = run(&["--migrate", SOURCE[0], SOURCE[1]], Some(&db.dsn)).await;
    assert_eq!(migrated.code, Some(0), "{}", migrated.stderr);
    let admin = PgPoolOptions::new().connect(&db.base).await.unwrap();
    // The worker's own table, so the store gate alone would not catch it.
    let latest = status(&migrated)["worker"]["latest"].as_i64().unwrap();
    let sql = format!(
        "INSERT INTO p11d_schema_cli_worker.linch_worker_migrations \
         (version, description, success, checksum, execution_time) \
         VALUES ({}, 'a later release', true, '\\x00', 0)",
        latest + 1
    );
    sqlx::query(AssertSqlSafe(sql))
        .execute(&admin)
        .await
        .unwrap();

    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("server.json");
    let config = serde_json::json!({"tenants": {}, "worker": {"dsn": {"value": db.dsn}}});
    std::fs::write(&file, config.to_string()).unwrap();
    let path = file.to_str().unwrap();
    let wait = std::time::Duration::from_secs(60);
    let started = tokio::time::timeout(wait, run(&["--config", path], None)).await;
    let started = started.expect("it refuses instead of serving");
    assert_eq!(started.code, Some(2), "{}", started.stderr);
    assert!(started.stderr.contains("newer"), "{}", started.stderr);
    // `--check --config` reads the same DSN and says the same.
    let checked = run(&["--check", "--config", path], None).await;
    assert_eq!(checked.code, Some(3), "{}", checked.stderr);
    db.drop().await;
}
