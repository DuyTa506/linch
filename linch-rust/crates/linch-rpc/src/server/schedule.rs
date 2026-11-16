//! The scheduler methods: `schedule.create`, `schedule.list`,
//! `schedule.pause`, `schedule.resume`, `schedule.delete`, `job.enqueue`,
//! `job.get` and `job.list`.
//!
//! Each names an open agent, whose Postgres pool holds the schedules and
//! jobs (in its database and schema) and whose namespace scopes them. A
//! schedule or a queued run stores the agent's config as it was opened,
//! without its store DSN, and a worker opens it again for every attempt
//! (see `background`); this connection never runs them.

use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, SecondsFormat, Utc};
use linch::postgres::PgPool;
use linch::{AgentConfig, JsonValue, StoreConfig};
use linch_worker::{Cap, RunRequest, Schedule, Timing, WorkerError};
use serde_json::json;
use tokio::sync::OnceCell;

use super::{Server, lock, to_value};
use crate::access::Tenant;
use crate::error::{ErrorObject, chain, code};
use crate::protocol::{
    JobEnqueueParams, JobGetParams, JobInfo, JobListParams, Jobs, ScheduleCreateParams,
    ScheduleDeleted, ScheduleIdParams, ScheduleInfo, ScheduleListParams, Schedules,
};

/// Jobs `job.list` returns when the call names no limit.
const DEFAULT_JOBS_LISTED: u32 = 50;

/// What an open agent needs to be scheduled: its definition, or why it
/// cannot run in the background, and whether its database has the queue
/// tables yet.
pub(super) struct Background {
    definition: Result<String, &'static str>,
    migrated: Arc<OnceCell<()>>,
}

impl Background {
    /// The definition of an agent opened from `config` (after the
    /// tenant's checks), with or without host tools and callbacks.
    pub(super) fn new(config: &AgentConfig, tools: bool, callbacks: bool) -> Self {
        Self {
            definition: definition(config, tools, callbacks),
            migrated: Arc::default(),
        }
    }
}

impl Default for Background {
    fn default() -> Self {
        Self {
            definition: Err("memory_store"),
            migrated: Arc::default(),
        }
    }
}

/// The stored definition of `config`: the config with its secrets (the
/// MCP env and header values it needs to open again) and without its
/// store DSN, which a worker replaces with its own.
fn definition(config: &AgentConfig, tools: bool, callbacks: bool) -> Result<String, &'static str> {
    if tools {
        return Err("host_tools");
    }
    if callbacks {
        return Err("callbacks");
    }
    if !matches!(config.stores, StoreConfig::Postgres { .. }) {
        return Err("memory_store");
    }
    let mut value = config.to_value_with_secrets().map_err(|_| "unencodable")?;
    if let Some(stores) = value.get_mut("stores").and_then(JsonValue::as_object_mut) {
        stores.insert("dsn".into(), JsonValue::String(String::new()));
    }
    serde_json::to_string(&value).map_err(|_| "unencodable")
}

/// An open agent, ready for a scheduler call.
struct Scheduled {
    pool: PgPool,
    namespace: String,
    definition: String,
}

impl Server {
    /// The agent `handle` as the scheduler needs it, with the queue tables
    /// in its database.
    async fn scheduled(&self, handle: &str) -> Result<Scheduled, ErrorObject> {
        let (pool, namespace, definition, migrated) = {
            let tables = lock(&self.0.tables);
            let opened = tables.opened(handle)?;
            let definition = opened.background.definition.clone().map_err(|reason| {
                let message = format!(
                    "agent {handle:?} cannot run in the background ({reason}): it needs \
                     Postgres stores and no host tools or callbacks"
                );
                ErrorObject::new(code::NOT_BACKGROUND, message)
                    .with_data(json!({ "reason": reason }))
            })?;
            let pool = opened.agent.pool().cloned();
            let pool = pool.expect("an agent with Postgres stores has a pool");
            let namespace = opened.agent.agent().namespace().to_owned();
            (
                pool,
                namespace,
                definition,
                Arc::clone(&opened.background.migrated),
            )
        };
        migrated
            .get_or_try_init(|| async { linch_worker::migrate(&pool).await })
            .await
            .map_err(|error| ErrorObject::new(code::STORE_UNAVAILABLE, chain(&error)))?;
        Ok(Scheduled {
            pool,
            namespace,
            definition,
        })
    }

    pub(super) async fn create_schedule(
        &self,
        params: ScheduleCreateParams,
    ) -> Result<JsonValue, ErrorObject> {
        let timing = timing(&params)?;
        self.admit_principal(params.principal.as_ref())?;
        let cap = match &self.0.tenant {
            Some(tenant) => Some(admit_schedule(tenant, &timing)?),
            None => None,
        };
        let agent = self.scheduled(&params.agent).await?;
        let id = (params.id).unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let schedule = Schedule {
            namespace: agent.namespace,
            id,
            queue: params.queue.unwrap_or_else(|| "default".into()),
            session_id: params.session_id,
            prompt: params.prompt,
            timing,
            agent: Some(agent.definition),
            principal: params.principal,
        };
        let created = linch_worker::create_schedule(&agent.pool, &schedule, cap.as_ref()).await;
        to_value(&schedule_info(
            created.map_err(|error| worker_error(&error))?,
        ))
    }

    pub(super) async fn list_schedules(
        &self,
        params: ScheduleListParams,
    ) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let listed = linch_worker::list_schedules(&agent.pool, &agent.namespace).await;
        let schedules = listed.map_err(|error| worker_error(&error))?;
        to_value(&Schedules {
            schedules: schedules.into_iter().map(schedule_info).collect(),
        })
    }

    pub(super) async fn pause_schedule(
        &self,
        params: ScheduleIdParams,
    ) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let paused = linch_worker::pause_schedule(&agent.pool, &agent.namespace, &params.id).await;
        found(paused, &params.id)
    }

    pub(super) async fn resume_schedule(
        &self,
        params: ScheduleIdParams,
    ) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let resumed =
            linch_worker::resume_schedule(&agent.pool, &agent.namespace, &params.id).await;
        found(resumed, &params.id)
    }

    pub(super) async fn delete_schedule(
        &self,
        params: ScheduleIdParams,
    ) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let deleted =
            linch_worker::delete_schedule(&agent.pool, &agent.namespace, &params.id).await;
        let deleted = deleted.map_err(|error| worker_error(&error))?;
        to_value(&ScheduleDeleted { deleted })
    }

    pub(super) async fn enqueue_job(
        &self,
        params: JobEnqueueParams,
    ) -> Result<JsonValue, ErrorObject> {
        self.admit_principal(params.principal.as_ref())?;
        let cap = match &self.0.tenant {
            Some(tenant) => Some(admit_job(tenant)?),
            None => None,
        };
        let agent = self.scheduled(&params.agent).await?;
        let request = RunRequest {
            queue: params.queue.unwrap_or_else(|| "default".into()),
            namespace: agent.namespace.clone(),
            session_id: params.session_id,
            prompt: params.prompt,
            agent: Some(agent.definition),
            principal: params.principal,
        };
        let id = linch_worker::enqueue(&agent.pool, &request, cap.as_ref()).await;
        let id = id.map_err(|error| worker_error(&error))?;
        let job = linch_worker::job(&agent.pool, &agent.namespace, id).await;
        let job = job.map_err(|error| worker_error(&error))?;
        // A worker may have finished it and its history been pruned since.
        let job = job.ok_or_else(|| job_not_found(id.0))?;
        to_value(&job_info(job))
    }

    pub(super) async fn get_job(&self, params: JobGetParams) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let id = linch_worker::JobId(params.job_id);
        let job = linch_worker::job(&agent.pool, &agent.namespace, id).await;
        let job = job.map_err(|error| worker_error(&error))?;
        to_value(&job_info(job.ok_or_else(|| job_not_found(params.job_id))?))
    }

    pub(super) async fn list_jobs(&self, params: JobListParams) -> Result<JsonValue, ErrorObject> {
        let agent = self.scheduled(&params.agent).await?;
        let limit = params.limit.unwrap_or(DEFAULT_JOBS_LISTED);
        let listed = linch_worker::jobs(
            &agent.pool,
            &agent.namespace,
            params.schedule_id.as_deref(),
            limit,
        )
        .await;
        let jobs = listed.map_err(|error| worker_error(&error))?;
        to_value(&Jobs {
            jobs: jobs.into_iter().map(job_info).collect(),
        })
    }
}

/// The timing `params` asks for: exactly one of `cron` and `interval_ms`,
/// and a time zone only with `cron`.
fn timing(params: &ScheduleCreateParams) -> Result<Timing, ErrorObject> {
    let invalid = |reason: &str| {
        ErrorObject::new(code::INVALID_SCHEDULE, reason.to_owned())
            .with_data(json!({ "reason": reason }))
    };
    let timing = match (&params.cron, params.interval_ms) {
        (Some(pattern), None) => Timing::Cron {
            pattern: pattern.clone(),
            timezone: params.timezone.clone().unwrap_or_else(|| "UTC".into()),
        },
        (None, Some(ms)) if params.timezone.is_none() => Timing::Every(Duration::from_millis(ms)),
        (None, Some(_)) => return Err(invalid("timezone is only for a cron schedule")),
        _ => return Err(invalid("give exactly one of cron and interval_ms")),
    };
    timing.check().map_err(|error| invalid(&chain(&error)))?;
    Ok(timing)
}

/// Refuse a schedule the tenant may not have; its cap otherwise.
fn admit_schedule(tenant: &Tenant, timing: &Timing) -> Result<Cap, ErrorObject> {
    let allow = tenant.allowlists();
    let Some(max) = allow.max_schedules else {
        let message = "the tenant's allowlists give it no schedules (max_schedules)";
        return Err(
            ErrorObject::new(code::NOT_ALLOWED, message).with_data(json!({ "kind": "schedules" }))
        );
    };
    let gap = timing.shortest_gap(Utc::now()).map_err(|error| {
        let reason = chain(&error);
        ErrorObject::new(code::INVALID_SCHEDULE, reason.clone())
            .with_data(json!({ "reason": reason }))
    })?;
    let min = allow.min_schedule_interval;
    if gap < min {
        let message = format!(
            "the schedule fires {} ms apart; the tenant's min_schedule_interval_ms is {}",
            gap.as_millis(),
            min.as_millis()
        );
        let data = json!({ "kind": "schedule_interval", "min_ms": min.as_millis() });
        return Err(ErrorObject::new(code::NOT_ALLOWED, message).with_data(data));
    }
    Ok(Cap {
        root: tenant.name().to_owned(),
        max,
    })
}

/// Refuse a background run the tenant may not queue; its cap otherwise.
fn admit_job(tenant: &Tenant) -> Result<Cap, ErrorObject> {
    let Some(max) = tenant.allowlists().max_queued_jobs else {
        let message = "the tenant's allowlists give it no background runs (max_queued_jobs)";
        return Err(ErrorObject::new(code::NOT_ALLOWED, message)
            .with_data(json!({ "kind": "background_runs" })));
    };
    Ok(Cap {
        root: tenant.name().to_owned(),
        max,
    })
}

fn found(
    schedule: Result<Option<linch_worker::ScheduleInfo>, WorkerError>,
    id: &str,
) -> Result<JsonValue, ErrorObject> {
    match schedule.map_err(|error| worker_error(&error))? {
        Some(schedule) => to_value(&schedule_info(schedule)),
        None => Err(ErrorObject::new(
            code::SCHEDULE_NOT_FOUND,
            format!("no schedule {id:?} in the agent's namespace"),
        )),
    }
}

fn job_not_found(id: i64) -> ErrorObject {
    ErrorObject::new(
        code::JOB_NOT_FOUND,
        format!("no job {id} in the agent's namespace"),
    )
}

/// The wire error of a scheduler call.
fn worker_error(error: &WorkerError) -> ErrorObject {
    match error {
        WorkerError::ScheduleExists { id } => ErrorObject::new(
            code::SCHEDULE_EXISTS,
            format!("schedule {id:?} already exists in the agent's namespace"),
        ),
        WorkerError::LimitReached { what, max } => {
            let message = format!("the tenant already holds its {max} {what}");
            ErrorObject::new(code::LIMIT_REACHED, message)
                .with_data(json!({ "kind": what, "max": max }))
        }
        WorkerError::Cron { .. } | WorkerError::Invalid(_) => {
            let reason = chain(error);
            ErrorObject::new(code::INVALID_SCHEDULE, reason.clone())
                .with_data(json!({ "reason": reason }))
        }
        other => ErrorObject::new(code::STORE, chain(other)),
    }
}

fn time(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn schedule_info(schedule: linch_worker::ScheduleInfo) -> ScheduleInfo {
    let (cron, timezone, interval_ms) = match schedule.timing {
        Timing::Cron { pattern, timezone } => (Some(pattern), Some(timezone), None),
        Timing::Every(every) => (None, None, u64::try_from(every.as_millis()).ok()),
    };
    ScheduleInfo {
        id: schedule.id,
        namespace: schedule.namespace,
        queue: schedule.queue,
        prompt: schedule.prompt,
        session_id: schedule.session_id,
        cron,
        timezone,
        interval_ms,
        paused: schedule.paused,
        next_fire: time(schedule.next_fire),
        last_fire: schedule.last_fire.map(time),
        fires: schedule.fires,
        skips: schedule.skips,
        created_at: time(schedule.created_at),
    }
}

fn job_info(job: linch_worker::JobInfo) -> JobInfo {
    JobInfo {
        job_id: job.id.0,
        queue: job.queue,
        state: job.state.as_str().to_owned(),
        session_id: job.session_id,
        run_id: job.run_id,
        schedule_id: job.schedule_id,
        fire_at: job.fire_at.map(time),
        attempts: job.attempts,
        failures: job.failures,
        error: job.error,
        created_at: time(job.created_at),
        finished_at: job.finished_at.map(time),
    }
}
