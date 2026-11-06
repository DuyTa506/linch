//! The summary figures of a [`RunReport`].

use std::collections::{BTreeMap, BTreeSet};

use linch_core::events::kinds::{ContextBuildEvent, ResultSubtype, VerificationAction};
use linch_core::{Event, JsonValue, Usage};
use serde::Serialize;

use super::tools::{ToolSummary, tool_summary};
use super::{RunReport, wire_name};

/// Counts, usage, tool timings, context, recovery and risk figures.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ReportSummary {
    /// Envelopes per event type.
    pub event_counts: BTreeMap<String, usize>,
    /// Token totals.
    pub usage: UsageSummary,
    /// Tool call counts and timings.
    pub tools: ToolSummary,
    /// Context budget use.
    pub context: ContextSummary,
    /// Compactions and verification retries.
    pub recovery: RecoverySummary,
    /// Prompt-cache advisories.
    pub prompt_cache: PromptCacheSummary,
    /// Counts of the events that need a look.
    pub risk: RiskSummary,
}

/// Token totals: the `result`'s, else the last `usage` event's cumulative.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct UsageSummary {
    /// Uncached input tokens.
    pub input_tokens: u64,
    /// Output tokens.
    pub output_tokens: u64,
    /// Input tokens read from the prompt cache.
    pub cache_read_tokens: u64,
    /// Input tokens written to the prompt cache.
    pub cache_creation_tokens: u64,
    /// All four buckets.
    pub total_tokens: u64,
    /// Cache reads over every input bucket, to 4 decimals.
    pub cache_read_ratio: f64,
}

/// Context budget use, from the `budget` of each `context_build`.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ContextSummary {
    /// Context builds.
    pub builds: usize,
    /// Builds whose budget reports `trimmed: true`.
    pub trimmed_builds: usize,
    /// Largest `used_tokens`.
    pub max_used_tokens: Option<i64>,
    /// Largest `max_tokens`.
    pub max_tokens_seen: Option<i64>,
    /// Largest `used_tokens / max_tokens`, to 4 decimals.
    pub max_utilization: Option<f64>,
    /// `over` above 1, `high` from 0.9, `moderate` from 0.75, else `none`.
    pub pressure: &'static str,
}

/// Compactions and verification retries.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct RecoverySummary {
    /// `compaction` events.
    pub compactions: usize,
    /// Tokens the compactions removed.
    pub compaction_tokens_saved: u64,
    /// `verification` events that retried.
    pub verification_retries: usize,
}

/// Prompt-cache advisories.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct PromptCacheSummary {
    /// As in [`UsageSummary::cache_read_ratio`].
    pub cache_read_ratio: f64,
    /// `prompt_cache_advisory` events.
    pub advisory_count: usize,
    /// Advisories per reason.
    pub reasons: BTreeMap<String, usize>,
    /// Advisories for a changed tool set.
    pub tool_selection_changes: usize,
}

/// Counts of the events that need a look.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct RiskSummary {
    /// Tool calls that asked for permission.
    pub permission_requests: usize,
    /// `loop_guard` events.
    pub loop_guards: usize,
    /// `error` events.
    pub errors: usize,
    /// `verification` events that retried.
    pub verification_retries: usize,
}

/// Signals for long runs.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct LongRunSummary {
    /// Context growth.
    pub context: LongRunContext,
    /// Whether the run went well.
    pub quality: QualitySummary,
    /// Where a resume would continue.
    pub resume: ResumeSummary,
}

/// Context growth over the run.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct LongRunContext {
    /// Context builds.
    pub builds: usize,
    /// Builds whose budget reports `trimmed: true`.
    pub trimmed_builds: usize,
    /// Largest `used_tokens`.
    pub max_used_tokens: Option<i64>,
    /// Builds that offered each tool.
    pub selected_tool_counts: BTreeMap<String, usize>,
    /// Every metadata key any build reported, sorted.
    pub metadata_keys: Vec<String>,
}

/// Whether the run went well.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct QualitySummary {
    /// Tool calls that failed.
    pub failed_tool_calls: usize,
    /// Whether the run ended with a successful `result`.
    pub completed: bool,
}

/// Where a resume would continue, from the record's checkpoint.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ResumeSummary {
    /// Whether the report has a checkpoint.
    pub has_checkpoint: bool,
    /// The checkpoint phase's wire tag.
    pub phase: Option<String>,
    /// The checkpoint's turn.
    pub turn_index: Option<u32>,
}

pub(super) fn summary(report: &RunReport) -> ReportSummary {
    let usage = usage_source(report);
    let tools = tool_summary(&report.tool_calls);
    let mut recovery = RecoverySummary::default();
    let mut prompt_cache = PromptCacheSummary::default();
    let mut event_counts = BTreeMap::new();
    for envelope in &report.timeline {
        *event_counts.entry(event_type(&envelope.event)).or_default() += 1;
        count_recovery(&envelope.event, &mut recovery, &mut prompt_cache);
    }
    prompt_cache.cache_read_ratio = cache_read_ratio(&usage);
    ReportSummary {
        event_counts,
        usage: UsageSummary {
            input_tokens: usage.input_tokens,
            output_tokens: usage.output_tokens,
            cache_read_tokens: usage.cache_read_tokens,
            cache_creation_tokens: usage.cache_creation_tokens,
            total_tokens: usage
                .total_input_tokens()
                .saturating_add(usage.output_tokens),
            cache_read_ratio: cache_read_ratio(&usage),
        },
        risk: RiskSummary {
            permission_requests: report.permission_requests.len(),
            loop_guards: report.loop_guards.len(),
            errors: report.errors.len(),
            verification_retries: recovery.verification_retries,
        },
        context: context_summary(&report.context_builds),
        tools,
        recovery,
        prompt_cache,
    }
}

pub(super) fn long_run(report: &RunReport) -> LongRunSummary {
    let builds = &report.context_builds;
    let mut selected_tool_counts = BTreeMap::new();
    let mut metadata_keys = BTreeSet::new();
    for build in builds {
        for tool in build.selected_tools.iter().flatten() {
            *selected_tool_counts.entry(tool.clone()).or_default() += 1;
        }
        metadata_keys.extend(build.metadata.keys().cloned());
    }
    let context = context_summary(builds);
    let checkpoint = report.checkpoint.as_ref();
    LongRunSummary {
        context: LongRunContext {
            builds: builds.len(),
            trimmed_builds: context.trimmed_builds,
            max_used_tokens: context.max_used_tokens,
            selected_tool_counts,
            metadata_keys: metadata_keys.into_iter().collect(),
        },
        quality: QualitySummary {
            failed_tool_calls: report.failed_tool_calls(),
            completed: report
                .final_result
                .as_ref()
                .is_some_and(|result| result.subtype == ResultSubtype::Success),
        },
        resume: ResumeSummary {
            has_checkpoint: checkpoint.is_some(),
            phase: report.phase.clone(),
            turn_index: checkpoint.map(|checkpoint| checkpoint.turn),
        },
    }
}

fn usage_source(report: &RunReport) -> Usage {
    match (&report.final_result, &report.usage) {
        (Some(result), _) => result.total_usage,
        (None, Some(usage)) => usage.cumulative,
        (None, None) => Usage::default(),
    }
}

fn cache_read_ratio(usage: &Usage) -> f64 {
    ratio(usage.cache_read_tokens, usage.total_input_tokens(), 4)
}

fn count_recovery(event: &Event, recovery: &mut RecoverySummary, cache: &mut PromptCacheSummary) {
    match event {
        Event::Compaction(compaction) => {
            recovery.compactions += 1;
            let saved = compaction
                .tokens_before
                .saturating_sub(compaction.tokens_after);
            recovery.compaction_tokens_saved += saved;
        }
        Event::Verification(verification) if verification.action == VerificationAction::Retry => {
            recovery.verification_retries += 1;
        }
        Event::PromptCacheAdvisory(advisory) => {
            let reason = wire_name(&advisory.reason);
            cache.tool_selection_changes += usize::from(reason == "tool_set_changed");
            *cache.reasons.entry(reason).or_default() += 1;
            cache.advisory_count += 1;
        }
        _ => {}
    }
}

fn context_summary(builds: &[ContextBuildEvent]) -> ContextSummary {
    let mut summary = ContextSummary {
        builds: builds.len(),
        ..ContextSummary::default()
    };
    let mut max_raw: Option<f64> = None;
    for build in builds {
        let budget = &build.budget;
        summary.trimmed_builds +=
            usize::from(budget.get("trimmed") == Some(&JsonValue::Bool(true)));
        let used = int(budget.get("used_tokens"));
        let maximum = int(budget.get("max_tokens"));
        summary.max_used_tokens = summary.max_used_tokens.max(used);
        summary.max_tokens_seen = summary.max_tokens_seen.max(maximum);
        if let (Some(used), Some(maximum)) = (used, maximum)
            && maximum > 0
        {
            let raw = used as f64 / maximum as f64;
            max_raw = Some(max_raw.map_or(raw, |seen| seen.max(raw)));
        }
    }
    summary.max_utilization = max_raw.map(|raw| round(raw, 4));
    summary.pressure = match max_raw {
        Some(raw) if raw > 1.0 => "over",
        Some(raw) if raw >= 0.9 => "high",
        Some(raw) if raw >= 0.75 => "moderate",
        _ => "none",
    };
    summary
}

/// A JSON integer, or a finite number truncated to one.
fn int(value: Option<&JsonValue>) -> Option<i64> {
    let value = value?;
    value.as_i64().or_else(|| {
        let number = value.as_f64()?;
        number.is_finite().then_some(number as i64)
    })
}

/// `part / whole` to `places` decimals; 0 when `whole` is 0.
pub(super) fn ratio(part: u64, whole: u64, places: i32) -> f64 {
    if whole == 0 {
        return 0.0;
    }
    round(part as f64 / whole as f64, places)
}

/// `value` to `places` decimals, half to even on its exact binary value,
/// like Python's `round`: formatting with a precision rounds that way.
fn round(value: f64, places: i32) -> f64 {
    let places = usize::try_from(places).unwrap_or(0);
    format!("{value:.places$}").parse().unwrap_or(value)
}

/// The event's wire `type`. Exhaustive, so a new kind must be named here.
fn event_type(event: &Event) -> String {
    let name = match event {
        Event::Init(_) => "init",
        Event::User(_) => "user",
        Event::Assistant(_) => "assistant",
        Event::PartialAssistant(_) => "partial_assistant",
        Event::Usage(_) => "usage",
        Event::ToolCallStart(_) => "tool_call_start",
        Event::ToolCallEnd(_) => "tool_call_end",
        Event::ToolProgress(_) => "tool_progress",
        Event::PermissionRequest(_) => "permission_request",
        Event::InputRequest(_) => "input_request",
        Event::Suspended(_) => "suspended",
        Event::ContextBuild(_) => "context_build",
        Event::Compaction(_) => "compaction",
        Event::LoopGuard(_) => "loop_guard",
        Event::Budget(_) => "budget",
        Event::PromptCacheAdvisory(_) => "prompt_cache_advisory",
        Event::SkillsLoaded(_) => "skills_loaded",
        Event::SkillInvoked(_) => "skill_invoked",
        Event::SkillCompleted(_) => "skill_completed",
        Event::Subagent(_) => "subagent",
        Event::ChildRunStatus(_) => "child_run_status",
        Event::Workflow(_) => "workflow",
        Event::Verification(_) => "verification",
        Event::Hook(_) => "hook",
        Event::Error(_) => "error",
        Event::Result(_) => "result",
        Event::ModelCallDelivery(_) => "model_call_delivery",
        Event::ModelCallReceipt(_) => "model_call_receipt",
        Event::ModelCallUsageSettlement(_) => "model_call_usage_settlement",
        Event::Ignorable(ignorable) => ignorable.type_name(),
    };
    name.to_owned()
}
