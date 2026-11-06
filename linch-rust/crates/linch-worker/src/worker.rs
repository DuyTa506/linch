//! [`Worker`]: the poll loop that fires schedules, dequeues jobs and runs
//! each one in its own task.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use linch_core::Agent;
use linch_postgres::{PgPool, PgRunStore};
use sqlx::postgres::PgListener;
use tokio::sync::Notify;
use tokio::task::{JoinError, JoinHandle, JoinSet};
use tokio_util::sync::CancellationToken;

use crate::CHANNEL;
use crate::error::{WorkerError, chain, non_blank};
use crate::execute::{Shared, execute};
use crate::lease::dequeue;
use crate::schedule::fire_due_schedules;

/// Pause after a failed `LISTEN` receive before trying again.
const LISTEN_RETRY: Duration = Duration::from_secs(1);

/// How a [`Worker`] takes and holds jobs.
#[derive(Debug, Clone)]
pub struct WorkerConfig {
    /// Owner name written with every run claim and job hold; unique per
    /// worker.
    pub owner: String,
    /// Queues to serve, one job from each in turn. A queue's concurrency
    /// limit is stored with the queue ([`crate::set_queue_limit`]).
    pub queues: Vec<String>,
    /// Most jobs this worker runs at once.
    pub max_jobs: usize,
    /// How long a dequeued job stays hidden without a heartbeat. The
    /// heartbeat runs every third of it.
    pub visibility_timeout: Duration,
    /// Longest wait between polls when no notification arrives; schedules
    /// are checked on every poll.
    pub poll_interval: Duration,
    /// Wait before a job's second attempt, after an error or while its
    /// session is busy; it doubles with every attempt, up to 5 minutes. A
    /// job whose attempts failed 5 times is failed for good, and so is its
    /// run; waiting for a busy session never counts.
    pub retry_delay: Duration,
}

impl WorkerConfig {
    /// Settings for `owner`: queue `default`, 32 jobs at once, a 30 s
    /// visibility timeout, a 1 s poll and a 5 s retry delay.
    pub fn new(owner: impl Into<String>) -> Self {
        Self {
            owner: owner.into(),
            queues: vec!["default".into()],
            max_jobs: 32,
            visibility_timeout: Duration::from_secs(30),
            poll_interval: Duration::from_secs(1),
            retry_delay: Duration::from_secs(5),
        }
    }

    fn check(&self) -> Result<(), WorkerError> {
        non_blank(&self.owner, "the owner")?;
        if self.queues.is_empty() {
            return Err(WorkerError::Invalid("a worker needs a queue".into()));
        }
        for queue in &self.queues {
            non_blank(queue, "a queue name")?;
        }
        if self.max_jobs == 0 {
            return Err(WorkerError::Invalid("max_jobs must be at least 1".into()));
        }
        let ms = Duration::from_millis(1);
        if self.visibility_timeout < ms * 3 || self.poll_interval < ms {
            let text = "the visibility timeout must be at least 3 ms and the poll interval 1 ms";
            return Err(WorkerError::Invalid(text.into()));
        }
        Ok(())
    }
}

/// A member of the worker fleet: serves the configured queues for the
/// namespaces it has an agent for.
///
/// The pool must have room for one listener connection plus the job
/// queries. Every agent must store its runs in this pool's database and
/// schema, through [`linch_postgres::PgRunStore`], because the worker reads
/// run records there to choose between start, resume and approve.
pub struct Worker {
    pool: PgPool,
    config: WorkerConfig,
    agents: HashMap<String, Agent>,
}

impl Worker {
    /// A worker on `pool` with `config` and no agents yet.
    pub fn new(pool: PgPool, config: WorkerConfig) -> Self {
        Self {
            pool,
            config,
            agents: HashMap::new(),
        }
    }

    /// Serve the jobs of [`Agent::namespace`] with `agent`, replacing an
    /// agent added before for the same namespace. Every worker serving the
    /// namespace needs an agent of the same contract.
    pub fn agent(mut self, agent: Agent) -> Self {
        self.agents.insert(agent.namespace().to_owned(), agent);
        self
    }

    /// Serve until `shutdown` is cancelled, then wait for the jobs in
    /// flight to finish.
    ///
    /// Dropping the returned future instead abandons the jobs in flight,
    /// like a crash: their runs stay resumable and the jobs become visible
    /// again after the visibility timeout. Database errors inside the loop
    /// are logged and retried on the next poll.
    ///
    /// Raises:
    ///     WorkerError: If the configuration is invalid, there is no agent,
    ///         or the `LISTEN` connection cannot be opened.
    pub async fn run(self, shutdown: CancellationToken) -> Result<(), WorkerError> {
        self.config.check()?;
        if self.agents.is_empty() {
            return Err(WorkerError::Invalid("a worker needs an agent".into()));
        }
        let mut listener = PgListener::connect_with(&self.pool).await?;
        listener.listen(CHANNEL).await?;
        let wake = Arc::new(Notify::new());
        let _listening = AbortOnDrop(tokio::spawn(listen(listener, Arc::clone(&wake))));
        let namespaces: Vec<String> = self.agents.keys().cloned().collect();
        let shared = Arc::new(Shared {
            pool: self.pool.clone(),
            runs: PgRunStore::new(self.pool.clone()),
            agents: self.agents,
            owner: self.config.owner.clone(),
            timeout: self.config.visibility_timeout,
            retry_delay: self.config.retry_delay,
        });
        let mut jobs = JoinSet::new();
        while !shutdown.is_cancelled() {
            poll(&self.config, &shared, &namespaces, &mut jobs).await;
            tokio::select! {
                () = shutdown.cancelled() => {}
                () = wake.notified() => {}
                Some(joined) = jobs.join_next(), if !jobs.is_empty() => report(joined),
                () = tokio::time::sleep(self.config.poll_interval) => {}
            }
        }
        while let Some(joined) = jobs.join_next().await {
            report(joined);
        }
        Ok(())
    }
}

/// Fire due schedules, then take one job from each queue in turn until the
/// worker is full or every queue is empty.
async fn poll(
    config: &WorkerConfig,
    shared: &Arc<Shared>,
    namespaces: &[String],
    jobs: &mut JoinSet<()>,
) {
    if let Err(error) = fire_due_schedules(&shared.pool).await {
        tracing::warn!(error = chain(&error), "firing schedules failed");
    }
    let mut queues: Vec<&String> = config.queues.iter().collect();
    while !queues.is_empty() {
        let mut i = 0;
        while i < queues.len() {
            if jobs.len() >= config.max_jobs {
                return;
            }
            let queue = queues[i];
            let taken = dequeue(
                &shared.pool,
                queue,
                namespaces,
                &config.owner,
                config.visibility_timeout,
            )
            .await;
            match taken {
                Ok(Some(lease)) => {
                    jobs.spawn(execute(Arc::clone(shared), lease));
                    i += 1;
                }
                Ok(None) => {
                    queues.remove(i);
                }
                Err(error) => {
                    tracing::warn!(%queue, error = chain(&error), "dequeue failed");
                    return;
                }
            }
        }
    }
}

fn report(joined: Result<(), JoinError>) {
    if let Err(error) = joined {
        // The job stays hidden until its timeout, then another attempt runs.
        tracing::warn!(error = chain(&error), "a job task panicked");
    }
}

/// Wake the poll loop on every notification. A notification that arrives
/// while the loop is busy is kept as the `Notify` permit.
async fn listen(mut listener: PgListener, wake: Arc<Notify>) {
    loop {
        match listener.recv().await {
            Ok(_) => wake.notify_one(),
            Err(error) => {
                tracing::warn!(error = chain(&error), "LISTEN failed; polling only");
                tokio::time::sleep(LISTEN_RETRY).await;
            }
        }
    }
}

/// Aborts the task when dropped, so an abandoned worker leaves no listener.
struct AbortOnDrop(JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_config_is_checked() {
        assert!(WorkerConfig::new("w1").check().is_ok());
        let mut blank = WorkerConfig::new(" ");
        assert!(blank.check().is_err());
        blank.owner = "w1".into();
        blank.queues.clear();
        assert!(blank.check().is_err());
        let mut zero = WorkerConfig::new("w1");
        zero.max_jobs = 0;
        assert!(zero.check().is_err());
        let mut fast = WorkerConfig::new("w1");
        fast.visibility_timeout = Duration::from_millis(2);
        assert!(fast.check().is_err());
    }
}
