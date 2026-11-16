//! Params and results of the scheduler methods: `schedule.*` and `job.*`.

use linch::{Principal, RunId, SessionId};
use serde::{Deserialize, Serialize};

/// Params of `schedule.create`.
///
/// Exactly one of `cron` and `interval_ms` is given. The schedule runs
/// with the agent's config as it was opened (its store DSN left out: a
/// worker connects with its own), stored with the schedule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleCreateParams {
    /// Agent handle from `agent.open`; an agent with Postgres stores and
    /// no host tools or callbacks.
    pub agent: String,
    /// The schedule id, unique in the agent's namespace; a fresh uuid
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// The user prompt of every fire's run (for a heartbeat, its check-in
    /// message).
    pub prompt: String,
    /// A cron pattern: five fields, or six with leading seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron: Option<String>,
    /// The IANA time zone `cron` is read in, such as `Europe/Berlin`;
    /// `UTC` when absent. Only with `cron`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timezone: Option<String>,
    /// Fire every this many milliseconds instead of on a cron pattern.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interval_ms: Option<u64>,
    /// The session every fire's run continues (a heartbeat): a fire is
    /// skipped while an earlier fire's job is unfinished or the session
    /// has a live run. Absent: each fire starts its own session,
    /// `schedule:<id>:<fire time>`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<SessionId>,
    /// The worker queue the fires go to; `default` when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue: Option<String>,
    /// Who every fire's run acts for, as for `run.start`: permission rules
    /// match on its roles. A tenant's must name the tenant.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `schedule.list`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleListParams {
    /// Agent handle; the schedules of its namespace are listed.
    pub agent: String,
}

/// Params of `schedule.pause`, `schedule.resume` and `schedule.delete`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleIdParams {
    /// Agent handle; the schedule is looked up in its namespace.
    pub agent: String,
    /// The schedule id.
    pub id: String,
}

/// A schedule, the result of `schedule.create`, `schedule.pause` and
/// `schedule.resume`. Times are RFC 3339 in UTC.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleInfo {
    /// Its id.
    pub id: String,
    /// Its namespace.
    pub namespace: String,
    /// Its queue.
    pub queue: String,
    /// The prompt of every fire.
    pub prompt: String,
    /// The session of every fire; absent when each fire starts its own.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<SessionId>,
    /// Its cron pattern, for a cron schedule.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cron: Option<String>,
    /// The time zone of `cron`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timezone: Option<String>,
    /// Its interval, for an interval schedule.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interval_ms: Option<u64>,
    /// Whether it is paused.
    pub paused: bool,
    /// Its next fire (while paused, the one it had when paused).
    pub next_fire: String,
    /// Its latest fire, made or skipped.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_fire: Option<String>,
    /// Fires that queued a run.
    pub fires: u64,
    /// Fires skipped because the session was busy.
    pub skips: u64,
    /// When it was created.
    pub created_at: String,
}

/// Result of `schedule.list`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Schedules {
    /// The namespace's schedules, by id.
    pub schedules: Vec<ScheduleInfo>,
}

/// Result of `schedule.delete`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleDeleted {
    /// Whether the namespace had the schedule; `false` is not an error.
    pub deleted: bool,
}

/// Params of `job.enqueue`: a one-off run in the background.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobEnqueueParams {
    /// Agent handle; as for `schedule.create`.
    pub agent: String,
    /// The session the run belongs to; created when missing.
    pub session_id: SessionId,
    /// The user prompt.
    pub prompt: String,
    /// The worker queue; `default` when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue: Option<String>,
    /// Who the run acts for; as for `schedule.create`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `job.get`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobGetParams {
    /// Agent handle; the job is looked up in its namespace.
    pub agent: String,
    /// The job id.
    pub job_id: i64,
}

/// Params of `job.list`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobListParams {
    /// Agent handle; the jobs of its namespace are listed.
    pub agent: String,
    /// Only the jobs this schedule made.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedule_id: Option<String>,
    /// The most jobs returned, newest first: 50 when absent, at most 500.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limit: Option<u32>,
}

/// A job, the result of `job.enqueue` and `job.get`. Times are RFC 3339 in
/// UTC.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobInfo {
    /// Its id.
    pub job_id: i64,
    /// Its queue.
    pub queue: String,
    /// `queued`, `running`, `retrying`, `failed`, `done` or `superseded`.
    pub state: String,
    /// The session of its run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<SessionId>,
    /// Its run, once a worker started it; read it with `run.events`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<RunId>,
    /// The schedule that made it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedule_id: Option<String>,
    /// The fire time it was made for.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fire_at: Option<String>,
    /// How many times a worker took it.
    pub attempts: u32,
    /// How many of those attempts failed.
    pub failures: u32,
    /// The last attempt's error, while retrying or failed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// When it was queued.
    pub created_at: String,
    /// When it ended.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finished_at: Option<String>,
}

/// Result of `job.list`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Jobs {
    /// The jobs, newest first.
    pub jobs: Vec<JobInfo>,
}
