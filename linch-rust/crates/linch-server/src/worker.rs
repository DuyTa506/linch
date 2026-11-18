//! [`BackgroundWorker`]: the worker a config's `worker` section runs in
//! the server process, firing schedules and running queued jobs with
//! [`BackgroundAgents`].

use std::fmt;
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Duration;

use linch::postgres::SchemaError;
use linch_rpc::{BackgroundAgents, Tenant};
use linch_worker::{Retention, Worker, WorkerConfig, WorkerError};
use thiserror::Error;
use tokio_util::sync::CancellationToken;

use crate::config::{RetentionSection, TokenSource, WorkerSection};

/// The worker could not start.
#[derive(Debug, Error)]
pub enum WorkerStartError {
    /// The DSN's environment variable is unset.
    #[error("worker: environment variable {0} is unset")]
    DsnEnv(String),
    /// A setting is out of range.
    #[error("worker: {0}")]
    Invalid(String),
    /// The database could not be reached or migrated.
    #[error("worker: cannot prepare the database: {0}")]
    Database(String),
    /// The database holds a migration this server does not embed: a newer
    /// release migrated it.
    #[error("worker: {0}")]
    SchemaNewer(SchemaError),
}

/// A worker serving the tenants' and the local roots' jobs in one
/// database, until its stop token fires.
pub struct BackgroundWorker {
    worker: Worker,
    roots: Vec<String>,
}

impl fmt::Debug for BackgroundWorker {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BackgroundWorker")
            .field("roots", &self.roots)
            .finish_non_exhaustive()
    }
}

impl BackgroundWorker {
    /// Connect to the section's database, apply the store and queue
    /// migrations, and set up a worker for every tenant in `tenants` and
    /// every local root.
    ///
    /// Args:
    ///     section: The config's `worker` section.
    ///     tenants: The tenants the listeners serve (share the same `Arc`s,
    ///         so background runs count against their run caps).
    ///     server_id: The server's name, the worker's owner name unless
    ///         the section sets one.
    ///     env: Looks up one environment variable, for the DSN.
    ///
    /// Raises:
    ///     WorkerStartError: If the DSN variable is unset, a setting is 0
    ///         or blank, or the database cannot be reached or migrated.
    pub async fn start(
        section: &WorkerSection,
        tenants: &[Arc<Tenant>],
        server_id: &str,
        env: impl Fn(&str) -> Option<String>,
    ) -> Result<Self, WorkerStartError> {
        let dsn = match &section.dsn {
            TokenSource::Value(dsn) => dsn.clone(),
            TokenSource::Env(var) => {
                env(var).ok_or_else(|| WorkerStartError::DsnEnv(var.clone()))?
            }
        };
        let size = match section.max_connections {
            Some(size) => NonZeroU32::new(size)
                .ok_or_else(|| WorkerStartError::Invalid("max_connections is 0".into()))?,
            None => linch::postgres::default_max_connections(),
        };
        let config = worker_config(section, server_id)?;
        let database = |error: &dyn std::error::Error| WorkerStartError::Database(chain(error));
        let pool = linch::postgres::connect(&dsn, linch::postgres::CONNECT_TIMEOUT, size)
            .await
            .map_err(|error| database(&error))?;
        // Before any migration or job: a database a newer release migrated
        // is not this server's to touch.
        let gate = |error: SchemaError| match error {
            SchemaError::Newer { .. } => WorkerStartError::SchemaNewer(error),
            other => database(&other),
        };
        linch::postgres::schema_version(&pool).await.map_err(gate)?;
        linch_worker::schema_version(&pool).await.map_err(gate)?;
        (linch::postgres::migrate(&pool).await).map_err(|error| database(&error))?;
        (linch_worker::migrate(&pool).await).map_err(|error| database(&error))?;
        let mut factory = BackgroundAgents::new(dsn);
        for tenant in tenants {
            factory = factory.tenant(Arc::clone(tenant));
        }
        for root in &section.local_namespaces {
            if root.is_empty() || root.contains('/') {
                let message = format!("local namespace root {root:?} must be one segment");
                return Err(WorkerStartError::Invalid(message));
            }
            factory = factory.local(root.clone());
        }
        let roots = factory.roots();
        let worker = Worker::new(pool, config)
            .job_caps(factory.job_caps())
            .definitions(roots.clone(), Arc::new(factory));
        Ok(Self { worker, roots })
    }

    /// The namespace roots whose jobs it runs.
    pub fn roots(&self) -> &[String] {
        &self.roots
    }

    /// Fire schedules and run jobs until `stop` fires, then wait for the
    /// jobs in flight.
    ///
    /// Raises:
    ///     WorkerError: If the `LISTEN` connection cannot be opened.
    pub async fn serve(self, stop: CancellationToken) -> Result<(), WorkerError> {
        self.worker.run(stop).await
    }
}

fn worker_config(
    section: &WorkerSection,
    server_id: &str,
) -> Result<WorkerConfig, WorkerStartError> {
    // Not a pid or a start time: a tenant reads this in a refused resume.
    let owner = section
        .owner
        .clone()
        .unwrap_or_else(|| server_id.to_owned());
    let mut config = WorkerConfig::new(owner);
    if let Some(queues) = &section.queues {
        config.queues.clone_from(queues);
    }
    let millis = |value: Option<u64>, field: &str| match value {
        Some(0) => Err(WorkerStartError::Invalid(format!("{field} is 0"))),
        Some(ms) => Ok(Some(Duration::from_millis(ms))),
        None => Ok(None),
    };
    if let Some(max) = section.max_jobs {
        config.max_jobs = max;
    }
    if let Some(every) = millis(section.poll_interval_ms, "poll_interval_ms")? {
        config.poll_interval = every;
    }
    if let Some(timeout) = millis(section.visibility_timeout_ms, "visibility_timeout_ms")? {
        config.visibility_timeout = timeout;
    }
    if let Some(delay) = millis(section.retry_delay_ms, "retry_delay_ms")? {
        config.retry_delay = delay;
    }
    if let Some(keep) = millis(section.history_retention_ms, "history_retention_ms")? {
        config.history_retention = Some(keep);
    }
    if let Some(drain) = millis(section.drain_timeout_ms, "drain_timeout_ms")? {
        config.drain_timeout = drain;
    }
    if let Some(section) = &section.retention {
        config.retention = Some(retention(section)?);
    }
    Ok(config)
}

/// The retention settings of `section`; each window is whole days, and a
/// zero or an unusable value is refused at startup.
fn retention(section: &RetentionSection) -> Result<Retention, WorkerStartError> {
    let days = |value: Option<u32>, field: &str| match value {
        Some(0) => Err(WorkerStartError::Invalid(format!("retention.{field} is 0"))),
        Some(days) => Ok(Some(Duration::from_secs(u64::from(days) * 86_400))),
        None => Ok(None),
    };
    let mut retention = Retention {
        terminal_runs: days(section.terminal_runs_days, "terminal_runs_days")?,
        idle_sessions: days(section.idle_sessions_days, "idle_sessions_days")?,
        ..Retention::default()
    };
    if let Some(period) = days(section.tombstone_days, "tombstone_days")? {
        retention.tombstones = period;
    }
    if let Some(batch) = section.batch_size {
        retention.batch_size = batch;
    }
    if let Some(secs) = section.interval_secs {
        retention.interval = Duration::from_secs(secs);
    }
    retention
        .check()
        .map_err(|error| WorkerStartError::Invalid(error.to_string()))?;
    Ok(retention)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn section(retention: &str) -> WorkerSection {
        let text = format!(r#"{{"dsn": {{"value": "postgres://x"}}, "retention": {retention}}}"#);
        serde_json::from_str(&text).unwrap()
    }

    fn config(retention: &str) -> Result<WorkerConfig, WorkerStartError> {
        worker_config(&section(retention), "srv")
    }

    #[test]
    fn retention_is_off_unless_the_section_is_there() {
        let text = r#"{"dsn": {"value": "postgres://x"}}"#;
        let section: WorkerSection = serde_json::from_str(text).unwrap();
        assert_eq!(worker_config(&section, "srv").unwrap().retention, None);
    }

    #[test]
    fn the_drain_timeout_is_30_seconds_unless_set_and_zero_is_refused_at_load() {
        let text = r#"{"dsn": {"value": "postgres://x"}}"#;
        let section: WorkerSection = serde_json::from_str(text).unwrap();
        let config = worker_config(&section, "srv").unwrap();
        assert_eq!(config.drain_timeout, Duration::from_secs(30));
        let text = r#"{"dsn": {"value": "postgres://x"}, "drain_timeout_ms": 2500}"#;
        let section: WorkerSection = serde_json::from_str(text).unwrap();
        let config = worker_config(&section, "srv").unwrap();
        assert_eq!(config.drain_timeout, Duration::from_millis(2500));
        let zero = r#"{"tenants": {}, "worker": {"dsn": {"value": "postgres://x"}, "drain_timeout_ms": 0}}"#;
        let error = crate::ServerConfig::from_json(zero)
            .unwrap_err()
            .to_string();
        assert!(error.contains("drain_timeout_ms is 0"), "{error}");
    }

    #[test]
    fn retention_windows_are_days_and_the_rest_defaults() {
        let retention = config(r#"{"terminal_runs_days": 14, "idle_sessions_days": 90}"#)
            .unwrap()
            .retention
            .unwrap();
        let day = Duration::from_secs(86_400);
        assert_eq!(retention.terminal_runs, Some(14 * day));
        assert_eq!(retention.idle_sessions, Some(90 * day));
        assert_eq!(retention.tombstones, linch_worker::TOMBSTONE_PERIOD);
        assert_eq!(retention.batch_size, linch_worker::BATCH_SIZE);
        assert_eq!(retention.interval, linch_worker::PASS_EVERY);
        let custom = config(
            r#"{"terminal_runs_days": 1, "tombstone_days": 2, "batch_size": 50, "interval_secs": 120}"#,
        )
        .unwrap()
        .retention
        .unwrap();
        assert_eq!(custom.idle_sessions, None);
        assert_eq!(
            (custom.tombstones, custom.batch_size, custom.interval),
            (2 * day, 50, Duration::from_secs(120))
        );
    }

    #[test]
    fn a_retention_that_deletes_nothing_or_cannot_work_is_refused_at_startup() {
        for (text, wanted) in [
            ("{}", "terminal_runs or idle_sessions"),
            (r#"{"terminal_runs_days": 0}"#, "terminal_runs_days is 0"),
            (
                r#"{"idle_sessions_days": 1, "tombstone_days": 0}"#,
                "tombstone_days is 0",
            ),
            (
                r#"{"idle_sessions_days": 1, "batch_size": 0}"#,
                "batch_size",
            ),
            (
                r#"{"idle_sessions_days": 1, "batch_size": 10001}"#,
                "batch_size",
            ),
            (
                r#"{"idle_sessions_days": 1, "interval_secs": 59}"#,
                "interval",
            ),
        ] {
            let error = config(text).unwrap_err().to_string();
            assert!(error.contains(wanted), "{text}: {error}");
        }
        let unknown: Result<WorkerSection, _> = serde_json::from_str(
            r#"{"dsn": {"value": "p"}, "retention": {"terminal_runs_days": 1, "days": 3}}"#,
        );
        assert!(unknown.is_err(), "a misspelled field is an error");
    }
}
