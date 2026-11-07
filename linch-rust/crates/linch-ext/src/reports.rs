//! The run report: one run's event stream folded into tool calls, errors,
//! usage and summary statistics, as JSON (serde) or Markdown.
//!
//! [`build_run_report`] reads any envelope list: a live
//! [`RunStream`](linch_core::RunStream)'s, an eval
//! [`CaseResult::events`](crate::evals::CaseResult::events), or a store's.
//! [`load_run_report`] reads one run from a [`RunStore`], with its record.
//! Building a report never fails.

mod markdown;
mod summary;
#[cfg(test)]
mod tests;
mod tools;

use std::collections::HashMap;

use linch_core::events::kinds::{
    ContextBuildEvent, LoopGuardEvent, PermissionRequestItem, ResultEvent, ResultSubtype,
    UsageEvent,
};
use linch_core::{
    Checkpoint, Envelope, Event, JsonObject, Persistence, RunId, RunRecord, RunStore, StoreError,
    ToolResultPart,
};
use serde::Serialize;

pub use summary::{
    ContextSummary, LongRunContext, LongRunSummary, PromptCacheSummary, QualitySummary,
    RecoverySummary, ReportSummary, ResumeSummary, RiskSummary, UsageSummary,
};
pub use tools::{ToolFailure, ToolSummary, ToolTiming};

/// One run, as a report. Its serde form is the JSON report.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RunReport {
    /// The run; from the record, else the `init` event, else empty.
    pub run_id: String,
    /// The run's session; from the record, else the `init` event, else
    /// empty.
    pub session_id: String,
    /// The record's status; without a record, `unknown`, then `running`
    /// after `init`, then the status of the last `suspended` event
    /// (`awaiting_approval`, `awaiting_input`) until a `result` sets the
    /// final one (`completed`, `error`, `aborted`, `interrupted`).
    pub status: String,
    /// The record's checkpoint phase, as its wire tag.
    pub phase: Option<String>,
    /// Number of envelopes read.
    pub event_count: usize,
    /// Every ended tool call, in end order.
    pub tool_calls: Vec<ToolCallRecord>,
    /// Every tool call that asked for permission.
    pub permission_requests: Vec<PermissionRequestItem>,
    /// Every `context_build` event.
    pub context_builds: Vec<ContextBuildEvent>,
    /// Every `loop_guard` event.
    pub loop_guards: Vec<LoopGuardEvent>,
    /// Every `error` event's payload.
    pub errors: Vec<JsonObject>,
    /// The last `usage` event.
    pub usage: Option<UsageEvent>,
    /// The `result` event.
    #[serde(rename = "final")]
    pub final_result: Option<ResultEvent>,
    /// The record's checkpoint.
    pub checkpoint: Option<Checkpoint>,
    /// Counts, usage, tool timings, context, recovery and risk figures.
    pub summary: ReportSummary,
    /// Signals for long runs: context growth, quality, resume state.
    pub long_run: LongRunSummary,
    /// Every envelope read, in order.
    pub timeline: Vec<Envelope>,
}

impl RunReport {
    /// Number of tool calls that ended in an error.
    pub fn failed_tool_calls(&self) -> usize {
        self.tool_calls.iter().filter(|call| call.is_error).count()
    }
}

/// Input and summary of each started call, by `tool_use_id`.
type Starts = HashMap<String, (JsonObject, String)>;

/// One tool call, from its `tool_call_start` and `tool_call_end` events.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolCallRecord {
    /// The call's id.
    pub tool_use_id: String,
    /// The tool.
    pub tool_name: String,
    /// The input; empty when the stream has no start for this call.
    pub input: JsonObject,
    /// The start's summary; empty when the stream has no start.
    pub summary: String,
    /// The result's text parts, joined.
    pub result: String,
    /// Whether the call failed.
    pub is_error: bool,
    /// How long the call ran, in milliseconds.
    pub duration_ms: u64,
}

/// Fold `events` (and the run's record, when known) into a report.
///
/// Args:
///     events: One run's envelopes, in order.
///     run: The run's record, for its ids, status and checkpoint.
///
/// Returns:
///     The report; never an error, whatever the stream holds.
pub fn build_run_report(events: &[Envelope], run: Option<&RunRecord>) -> RunReport {
    let mut report = RunReport {
        run_id: run.map(|run| run.run_id.to_string()).unwrap_or_default(),
        session_id: run
            .map(|run| run.session_id.to_string())
            .unwrap_or_default(),
        status: run.map_or_else(|| "unknown".to_owned(), |run| wire_name(&run.status)),
        phase: run.map(|run| phase_name(&run.checkpoint)),
        event_count: events.len(),
        tool_calls: Vec::new(),
        permission_requests: Vec::new(),
        context_builds: Vec::new(),
        loop_guards: Vec::new(),
        errors: Vec::new(),
        usage: None,
        final_result: None,
        checkpoint: run.map(|run| run.checkpoint.clone()),
        summary: ReportSummary::default(),
        long_run: LongRunSummary::default(),
        timeline: events.to_vec(),
    };
    let mut starts = HashMap::new();
    for envelope in events {
        report.fold(&envelope.event, run.is_some(), &mut starts);
    }
    report.long_run = summary::long_run(&report);
    report.summary = summary::summary(&report);
    report
}

impl RunReport {
    /// Fold one event in; `starts` holds the input and summary of each
    /// tool call not yet ended.
    fn fold(&mut self, event: &Event, has_record: bool, starts: &mut Starts) {
        // A suspend is not final: an approved run's later `result` still
        // sets the status. Only a `result` closes it.
        let open = !has_record
            && matches!(
                self.status.as_str(),
                "unknown" | "running" | "awaiting_approval" | "awaiting_input"
            );
        match event {
            Event::Init(init) => {
                if self.run_id.is_empty() {
                    self.run_id = init.run_id.to_string();
                }
                if self.session_id.is_empty() {
                    self.session_id = init.session_id.to_string();
                }
                if self.status == "unknown" {
                    self.status = "running".to_owned();
                }
            }
            Event::ToolCallStart(start) => {
                let started = (start.input.clone(), start.summary.clone());
                starts.insert(start.tool_use_id.clone(), started);
            }
            Event::ToolCallEnd(end) => {
                let (input, summary) = starts.remove(end.tool_use_id()).unwrap_or_default();
                self.tool_calls.push(ToolCallRecord {
                    tool_use_id: end.tool_use_id().to_owned(),
                    tool_name: end.tool_name.clone(),
                    input,
                    summary,
                    result: result_text(&end.result.content),
                    is_error: end.is_error(),
                    duration_ms: end.duration_ms,
                });
            }
            Event::PermissionRequest(request) => {
                self.permission_requests
                    .extend(request.requests.iter().cloned());
            }
            Event::ContextBuild(build) => self.context_builds.push(build.clone()),
            Event::LoopGuard(guard) => self.loop_guards.push(guard.clone()),
            Event::Error(error) => self.errors.push(error.error.clone()),
            Event::Usage(usage) => self.usage = Some(usage.clone()),
            Event::Result(result) => {
                if open {
                    self.status = match result.subtype {
                        ResultSubtype::Success => "completed".to_owned(),
                        other => wire_name(&other),
                    };
                }
                self.final_result = Some(result.clone());
            }
            Event::Suspended(suspended) if open => self.status = wire_name(&suspended.status),
            _ => {}
        }
    }
}

/// Load run `run_id` of `namespace` and its events from `runs`, and report
/// on them. The journal-only kinds (model-call deliveries and receipts)
/// are left out, as [`Agent::events`](linch_core::Agent::events) leaves
/// them out.
///
/// Raises:
///     StoreError: If the run does not exist or a store read fails.
pub async fn load_run_report(
    runs: &dyn RunStore,
    namespace: &str,
    run_id: &RunId,
) -> Result<RunReport, StoreError> {
    let record = runs.load(namespace, run_id).await?;
    let events: Vec<Envelope> = runs
        .events(namespace, run_id, 0)
        .await?
        .into_iter()
        .filter(|envelope| envelope.event.persistence() != Persistence::JournalOnly)
        .collect();
    Ok(build_run_report(&events, Some(&record)))
}

fn result_text(content: &[ToolResultPart]) -> String {
    content
        .iter()
        .filter_map(|part| match part {
            ToolResultPart::Text(text) => Some(text.text.as_str()),
            ToolResultPart::Image(_) => None,
        })
        .collect()
}

/// A unit enum's snake-case wire name.
fn wire_name<T: Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(name)) => name,
        _ => String::new(),
    }
}

/// The checkpoint phase's wire tag.
fn phase_name(checkpoint: &Checkpoint) -> String {
    let value = serde_json::to_value(&checkpoint.phase).unwrap_or_default();
    value["phase"].as_str().unwrap_or_default().to_owned()
}
