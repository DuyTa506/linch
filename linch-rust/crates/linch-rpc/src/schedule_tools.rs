//! The schedule tools of an agent opened with `schedule_tools`: the
//! [`ScheduleBackend`] that binds `CreateSchedule`, `ListSchedules` and
//! `CancelSchedule` to that agent, for `agent.open` and for the
//! [`BackgroundAgents`](crate::BackgroundAgents) a worker opens.
//!
//! What the model cannot choose, the binding fixes:
//! - every schedule runs the agent's own stored definition (the one
//!   `schedule.create` stores), never a config from tool input;
//! - it acts for the calling run's principal, admitted by the tenant;
//! - it passes the same admission as `schedule.create`
//!   ([`admit_create`]): the tenant's `max_schedules` cap and
//!   `min_schedule_interval_ms` floor;
//! - list and cancel see only the namespace's schedules of that same
//!   definition in the calling run's session;
//! - a subagent's call is refused: a child with fewer tools would schedule
//!   the whole agent.
//!
//! The tools go into the registry before the agent is built, so the
//! contract covers them; the binding is filled once the open made the
//! agent's pool, before any run can call them.

use std::sync::{Arc, OnceLock};

use async_trait::async_trait;
use linch::postgres::{PgPool, PgRunStore};
use linch::tools::{NewSchedule, ScheduleBackend, ScheduleEntry, ScheduleTiming, schedule_tools};
use linch::{RunStore, SessionId, ToolOutputError, ToolRegistry};
use linch_worker::{Schedule, ScheduleInfo};
use tokio::sync::OnceCell;

use crate::access::Tenant;
use crate::error::{ErrorObject, chain, code};
use crate::server::schedule::{Asked, admit_create, store_schedule, time, worker_error};

/// The agent a set of schedule tools is bound to.
pub(crate) struct Bound {
    /// The agent's pool: its database and schema hold the schedules.
    pub(crate) pool: PgPool,
    /// The agent's namespace.
    pub(crate) namespace: String,
    /// The agent's stored definition, as `schedule.create` stores it.
    pub(crate) definition: String,
    /// The tenant whose caps hold.
    pub(crate) tenant: Arc<Tenant>,
    /// Migrates the queue tables into the pool's database once.
    pub(crate) migrated: Arc<OnceCell<()>>,
}

/// The schedule tools' backend, bound once the agent is open.
#[derive(Clone, Default)]
pub(crate) struct Binding(Arc<OnceLock<Bound>>);

impl Binding {
    /// Register the three schedule tools into `tools`, unbound.
    ///
    /// Raises:
    ///     ErrorObject: `-32011` if a tool of the same name is registered.
    pub(crate) fn register(tools: &ToolRegistry) -> Result<Self, ErrorObject> {
        let binding = Self::default();
        let backend: Arc<dyn ScheduleBackend> = Arc::new(binding.clone());
        for tool in schedule_tools(backend) {
            // The registry lives as long as the agent; the handle is not needed.
            let _ = tools.register(tool).map_err(|error| {
                ErrorObject::new(code::BUILD_INVALID, format!("schedule_tools: {error}"))
            })?;
        }
        Ok(binding)
    }

    /// Bind the tools to `bound`; a second bind is ignored.
    pub(crate) fn bind(&self, bound: Bound) {
        let _ = self.0.set(bound);
    }

    fn bound(&self) -> Result<&Bound, ToolOutputError> {
        (self.0.get()).ok_or_else(|| tool_error("the schedule tools are not bound yet", "store"))
    }
}

impl Bound {
    async fn migrated(&self) -> Result<(), ToolOutputError> {
        let migrated = self
            .migrated
            .get_or_try_init(|| async { linch_worker::migrate(&self.pool).await });
        (migrated.await)
            .map(|_| ())
            .map_err(|error| tool_error(&chain(&error), "store"))
    }

    /// Refuse a call from a child run: a subagent may hold fewer tools
    /// than the agent its schedule would run.
    async fn refuse_child(&self, run: &linch::RunId) -> Result<(), ToolOutputError> {
        let runs = PgRunStore::new(self.pool.clone());
        match runs.load(&self.namespace, run).await {
            Ok(record) if record.parent_run_id.is_some() => Err(tool_error(
                "a subagent cannot create schedules",
                "not_allowed",
            )),
            Ok(_) => Ok(()),
            // Fail closed: a call's run is durable before the call starts.
            Err(error) => Err(tool_error(&error.to_string(), "store")),
        }
    }

    /// The schedule `id` of this agent in `session`, if it exists.
    async fn own(
        &self,
        session: &SessionId,
        id: &str,
    ) -> Result<Option<ScheduleInfo>, ToolOutputError> {
        let (pool, namespace) = (&self.pool, &self.namespace);
        let listed =
            linch_worker::list_agent_schedules(pool, namespace, &self.definition, session).await;
        let listed = listed.map_err(|error| refusal(&worker_error(&error)))?;
        Ok(listed.into_iter().find(|schedule| schedule.id == id))
    }
}

#[async_trait]
impl ScheduleBackend for Binding {
    async fn create(&self, new: NewSchedule) -> Result<ScheduleEntry, ToolOutputError> {
        let bound = self.bound()?;
        bound.refuse_child(&new.run_id).await?;
        let (cron, timezone, interval_ms) = match &new.timing {
            ScheduleTiming::Cron { pattern, timezone } => {
                (Some(pattern.as_str()), timezone.as_deref(), None)
            }
            ScheduleTiming::Interval(every) => {
                let ms = u64::try_from(every.as_millis()).unwrap_or(u64::MAX);
                (None, None, Some(ms))
            }
        };
        let asked = Asked {
            cron,
            timezone,
            interval_ms,
        };
        let tenant = Some(bound.tenant.as_ref());
        let (timing, cap) =
            admit_create(tenant, asked, new.principal.as_ref()).map_err(|error| refusal(&error))?;
        bound.migrated().await?;
        // The call's key as the id: a retry of the same call finds its
        // schedule instead of making a second one.
        if let Some(made) = bound.own(&new.session_id, &new.call_key).await? {
            return Ok(entry(made));
        }
        let schedule = Schedule {
            namespace: bound.namespace.clone(),
            id: new.call_key,
            queue: "default".into(),
            session_id: Some(new.session_id),
            prompt: new.prompt,
            timing,
            agent: Some(bound.definition.clone()),
            principal: new.principal,
        };
        let created = store_schedule(&bound.pool, &schedule, cap.as_ref()).await;
        Ok(entry(created.map_err(|error| refusal(&error))?))
    }

    async fn list(&self, session: &SessionId) -> Result<Vec<ScheduleEntry>, ToolOutputError> {
        let bound = self.bound()?;
        bound.migrated().await?;
        let (pool, namespace) = (&bound.pool, &bound.namespace);
        let listed =
            linch_worker::list_agent_schedules(pool, namespace, &bound.definition, session).await;
        let listed = listed.map_err(|error| refusal(&worker_error(&error)))?;
        Ok(listed.into_iter().map(entry).collect())
    }

    async fn cancel(&self, session: &SessionId, id: &str) -> Result<bool, ToolOutputError> {
        let bound = self.bound()?;
        bound.migrated().await?;
        let deleted = linch_worker::delete_agent_schedule(
            &bound.pool,
            &bound.namespace,
            id,
            &bound.definition,
            session,
        )
        .await;
        deleted.map_err(|error| refusal(&worker_error(&error)))
    }
}

fn entry(schedule: ScheduleInfo) -> ScheduleEntry {
    ScheduleEntry {
        id: schedule.id,
        prompt: schedule.prompt,
        paused: schedule.paused,
        next_fire: time(schedule.next_fire),
    }
}

fn tool_error(message: &str, code: &str) -> ToolOutputError {
    ToolOutputError {
        message: message.to_owned(),
        code: Some(code.to_owned()),
        details: None,
    }
}

/// A refused or failed scheduler call as the model sees it: the message,
/// a code named after the wire error, and its data.
fn refusal(error: &ErrorObject) -> ToolOutputError {
    let name = match error.code {
        code::NOT_ALLOWED => "not_allowed",
        code::LIMIT_REACHED => "limit_reached",
        code::INVALID_SCHEDULE => "invalid_schedule",
        _ => "store",
    };
    ToolOutputError {
        message: error.message.clone(),
        code: Some(name.to_owned()),
        details: error.data.clone(),
    }
}
