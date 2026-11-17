//! The schedule tools `CreateSchedule`, `ListSchedules` and
//! `CancelSchedule`, over a [`ScheduleBackend`] the host binds.
//!
//! Names and model-facing texts are Python's (`register_schedule_tools`).
//! The model chooses only the prompt and the timing. The host chooses
//! everything else: the agent definition a fire runs, the namespace, the
//! queue, the schedule id, and the caps. Every fire continues the calling
//! run's session and acts for the calling run's principal.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, Principal, RunId, SessionId, Tool, ToolContext, ToolOutput,
    ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

use crate::input::{coded, invalid, optional_string, schema, string};

/// The tool names, in [`schedule_tools`] order.
pub const SCHEDULE_TOOL_NAMES: [&str; 3] = ["CreateSchedule", "ListSchedules", "CancelSchedule"];

/// When a model-created schedule fires.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleTiming {
    /// A cron pattern, read in `timezone` (UTC when `None`).
    Cron {
        /// Five fields, or six with leading seconds.
        pattern: String,
        /// IANA time zone name.
        timezone: Option<String>,
    },
    /// A fixed interval.
    Interval(Duration),
}

/// What a `CreateSchedule` call asks the backend to store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewSchedule {
    /// The user prompt of every fire's run.
    pub prompt: String,
    /// When it fires.
    pub timing: ScheduleTiming,
    /// The calling run's session, which every fire continues.
    pub session_id: SessionId,
    /// The calling run.
    pub run_id: RunId,
    /// The call's [`ToolContext::idempotency_key`]: the same for a retry
    /// of the same call, so a backend can store it once.
    pub call_key: String,
    /// The calling run's principal, which every fire acts for.
    pub principal: Option<Principal>,
}

/// A stored schedule as the model sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleEntry {
    /// Schedule id.
    pub id: String,
    /// The prompt of every fire.
    pub prompt: String,
    /// Whether it is paused.
    pub paused: bool,
    /// Its next fire, RFC 3339.
    pub next_fire: String,
}

/// Where the schedule tools store and find schedules. The host binds it
/// to one agent: its definition, namespace and caps. Errors are what the
/// model sees.
#[async_trait]
pub trait ScheduleBackend: Send + Sync {
    /// Store `schedule` for the bound agent, under the host's caps.
    async fn create(&self, schedule: NewSchedule) -> Result<ScheduleEntry, ToolOutputError>;

    /// The bound agent's schedules.
    async fn list(&self) -> Result<Vec<ScheduleEntry>, ToolOutputError>;

    /// Delete the bound agent's schedule `id`; `false` when it has none.
    async fn cancel(&self, id: &str) -> Result<bool, ToolOutputError>;
}

/// `CreateSchedule`, `ListSchedules` and `CancelSchedule` over `backend`.
pub fn schedule_tools(backend: Arc<dyn ScheduleBackend>) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(CreateSchedule::new(Arc::clone(&backend))),
        Arc::new(ListSchedules::new(Arc::clone(&backend))),
        Arc::new(CancelSchedule::new(backend)),
    ]
}

fn text(text: String) -> ToolResult {
    Ok(ToolOutput::new(text))
}

/// The timing a `CreateSchedule` input asks for: exactly one of `cron`
/// and `interval_s`, and `timezone` only with `cron`.
fn timing(input: &JsonObject) -> Result<ScheduleTiming, ToolOutputError> {
    let cron = optional_string(input, "cron")?;
    let timezone = optional_string(input, "timezone")?;
    let interval = match input.get("interval_s") {
        None | Some(JsonValue::Null) => None,
        Some(value) => Some(
            value
                .as_f64()
                .ok_or_else(|| invalid("interval_s must be a number"))?,
        ),
    };
    match (cron, interval) {
        (Some(pattern), None) => Ok(ScheduleTiming::Cron {
            pattern: pattern.to_owned(),
            timezone: timezone.map(str::to_owned),
        }),
        (None, Some(_)) if timezone.is_some() => {
            Err(invalid("timezone is only for a cron schedule"))
        }
        (None, Some(seconds)) => {
            let ms = seconds * 1000.0;
            if !ms.is_finite() || ms < 1.0 || ms > u64::MAX as f64 {
                return Err(invalid("interval_s must be positive and finite"));
            }
            Ok(ScheduleTiming::Interval(Duration::from_millis(
                ms.round() as u64
            )))
        }
        _ => Err(invalid("provide exactly one of cron or interval_s")),
    }
}

fn payload(input: &JsonObject) -> Result<&str, ToolOutputError> {
    let payload = string(input, "payload")?;
    if payload.trim().is_empty() {
        return Err(invalid("payload must be a non-empty string"));
    }
    Ok(payload)
}

/// Schedule recurring runs of this agent in the calling session. Scope
/// `exec`: each fire runs the agent again.
pub struct CreateSchedule {
    backend: Arc<dyn ScheduleBackend>,
    schema: JsonObject,
}

impl CreateSchedule {
    /// A `CreateSchedule` over `backend`.
    pub fn new(backend: Arc<dyn ScheduleBackend>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "payload": {
                    "type": "string",
                    "description": "The prompt each scheduled run starts with.",
                },
                "cron": {
                    "type": "string",
                    "description": "A five-field cron pattern (six with leading seconds).",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA time zone the cron pattern is read in; UTC when absent.",
                },
                "interval_s": {
                    "type": "number",
                    "description": "Fire every this many seconds instead of on a cron pattern.",
                },
            },
            "required": ["payload"],
            "additionalProperties": false,
        }));
        Self { backend, schema }
    }
}

#[async_trait]
impl Tool for CreateSchedule {
    fn name(&self) -> &str {
        SCHEDULE_TOOL_NAMES[0]
    }

    fn description(&self) -> &str {
        "Schedule recurring work in this session using a five-field cron pattern \
         (UTC unless timezone is given) or an interval in seconds."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        payload(&input)?;
        timing(&input)?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let when = match timing(input) {
            Ok(ScheduleTiming::Cron { pattern, .. }) => format!("cron {pattern}"),
            Ok(ScheduleTiming::Interval(every)) => format!("every {} s", every.as_secs_f64()),
            Err(_) => "?".to_owned(),
        };
        format!("Create schedule ({when})")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let schedule = NewSchedule {
            prompt: payload(&input)?.to_owned(),
            timing: timing(&input)?,
            call_key: ctx.idempotency_key(),
            session_id: ctx.session_id,
            run_id: ctx.run_id,
            principal: ctx.principal,
        };
        let made = self.backend.create(schedule).await?;
        let mut output = ToolOutput::new(format!("Scheduled {} at {}", made.id, made.next_fire));
        output.metadata.insert("id".into(), made.id.into());
        output
            .metadata
            .insert("next_fire".into(), made.next_fire.into());
        Ok(output)
    }
}

/// List this agent's schedules. Scope `read`.
pub struct ListSchedules {
    backend: Arc<dyn ScheduleBackend>,
    schema: JsonObject,
}

impl ListSchedules {
    /// A `ListSchedules` over `backend`.
    pub fn new(backend: Arc<dyn ScheduleBackend>) -> Self {
        let schema = schema(json!({"type": "object", "properties": {}}));
        Self { backend, schema }
    }
}

#[async_trait]
impl Tool for ListSchedules {
    fn name(&self) -> &str {
        SCHEDULE_TOOL_NAMES[1]
    }

    fn description(&self) -> &str {
        "List this agent's schedule definitions."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, _input: &JsonObject) -> String {
        "List schedules".to_owned()
    }

    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let schedules = self.backend.list().await?;
        if schedules.is_empty() {
            return text("No schedules registered.".to_owned());
        }
        let line = |s: &ScheduleEntry| {
            let enabled = !s.paused;
            format!(
                "{}: {} enabled={enabled} next_fire={}",
                s.id, s.prompt, s.next_fire
            )
        };
        text(schedules.iter().map(line).collect::<Vec<_>>().join("\n"))
    }
}

/// Cancel one of this agent's schedules; runs it already queued finish.
/// Scope `write`.
pub struct CancelSchedule {
    backend: Arc<dyn ScheduleBackend>,
    schema: JsonObject,
}

impl CancelSchedule {
    /// A `CancelSchedule` over `backend`.
    pub fn new(backend: Arc<dyn ScheduleBackend>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": false,
        }));
        Self { backend, schema }
    }
}

#[async_trait]
impl Tool for CancelSchedule {
    fn name(&self) -> &str {
        SCHEDULE_TOOL_NAMES[2]
    }

    fn description(&self) -> &str {
        "Cancel a schedule belonging to this agent."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        string(&input, "id")?;
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let id = input.get("id").and_then(JsonValue::as_str).unwrap_or("");
        format!("Cancel schedule {id}")
    }

    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let id = string(&input, "id")?;
        if self.backend.cancel(id).await? {
            text(format!("Cancelled {id}"))
        } else {
            Err(coded(format!("No schedule {id}"), "not_found"))
        }
    }
}
