//! [`BackgroundWorker`]: the worker a config's `worker` section runs in
//! the server process, firing schedules and running queued jobs with
//! [`BackgroundAgents`].

use std::fmt;
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use linch_rpc::{BackgroundAgents, Tenant};
use linch_worker::{Worker, WorkerConfig, WorkerError};
use thiserror::Error;
use tokio_util::sync::CancellationToken;

use crate::config::{TokenSource, WorkerSection};

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
    ///     env: Looks up one environment variable, for the DSN.
    ///
    /// Raises:
    ///     WorkerStartError: If the DSN variable is unset, a setting is 0
    ///         or blank, or the database cannot be reached or migrated.
    pub async fn start(
        section: &WorkerSection,
        tenants: &[Arc<Tenant>],
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
        let config = worker_config(section)?;
        let database = |error: &dyn std::error::Error| WorkerStartError::Database(chain(error));
        let pool = linch::postgres::connect(&dsn, linch::postgres::CONNECT_TIMEOUT, size)
            .await
            .map_err(|error| database(&error))?;
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

fn worker_config(section: &WorkerSection) -> Result<WorkerConfig, WorkerStartError> {
    let owner = section.owner.clone().unwrap_or_else(|| {
        let started = SystemTime::now().duration_since(UNIX_EPOCH);
        let nanos = started.map_or(0, |since| since.as_nanos());
        format!("linch-server-{}-{nanos}", std::process::id())
    });
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
    Ok(config)
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
