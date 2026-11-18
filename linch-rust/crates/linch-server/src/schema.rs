//! `linch-server --migrate` and `--check`: apply or read the database
//! schema as a deploy step, with an exit status a script can branch on.

use linch::postgres::SchemaError;
use linch::{SchemaStatus, check, migrate};
use serde_json::json;

/// What a schema command does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchemaAction {
    /// Apply every migration this binary embeds.
    Migrate,
    /// Report what is applied, writing nothing.
    Check,
}

/// Exit status: the schema is current.
pub const EXIT_CURRENT: u8 = 0;
/// Exit status: the database could not be reached, or a migration failed.
pub const EXIT_FAILED: u8 = 1;
/// Exit status: the database holds a migration this binary does not know.
pub const EXIT_NEWER: u8 = 3;
/// Exit status of `--check`: the schema is behind this binary; run
/// `--migrate`.
pub const EXIT_BEHIND: u8 = 4;

/// What a schema command prints and how it exits.
#[derive(Debug, PartialEq, Eq)]
pub struct SchemaOutcome {
    /// The exit status.
    pub code: u8,
    /// One JSON line for stdout; empty when the command failed.
    pub stdout: String,
    /// The reason, for stderr; empty when the command succeeded.
    pub stderr: String,
}

/// Run `action` on the database of `dsn`.
///
/// Args:
///     action: Whether to migrate or only check.
///     dsn: The connection string the agents' `stores.dsn` uses.
///
/// Returns:
///     The exit status and what to print. stdout is one JSON line:
///     `{"current": bool, "store": {"applied": n|null, "latest": n},
///     "worker": {...}}`. No text holds the DSN.
pub async fn run_schema(action: SchemaAction, dsn: &str) -> SchemaOutcome {
    let status = match action {
        SchemaAction::Migrate => migrate(dsn).await,
        SchemaAction::Check => check(dsn).await,
    };
    match status {
        Ok(status) => {
            let code = if status.is_current() {
                EXIT_CURRENT
            } else {
                EXIT_BEHIND
            };
            SchemaOutcome {
                code,
                stdout: report(&status),
                stderr: String::new(),
            }
        }
        Err(error) => SchemaOutcome {
            code: match error {
                SchemaError::Newer { .. } => EXIT_NEWER,
                _ => EXIT_FAILED,
            },
            stdout: String::new(),
            stderr: chain(&error),
        },
    }
}

fn report(status: &SchemaStatus) -> String {
    let version =
        |v: &linch::postgres::SchemaVersion| json!({ "applied": v.applied, "latest": v.latest });
    json!({
        "current": status.is_current(),
        "store": version(&status.store),
        "worker": version(&status.worker),
    })
    .to_string()
}

fn chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(": ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}
