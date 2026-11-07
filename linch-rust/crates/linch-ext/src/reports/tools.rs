//! The tool figures of a [`RunReport`](super::RunReport).

use std::collections::BTreeMap;

use serde::Serialize;

use super::ToolCallRecord;
use super::summary::ratio;

#[cfg(test)]
mod tests;

/// How many calls the slowest and failing tool lists hold.
const TOP: usize = 5;

/// Tool call counts and timings.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ToolSummary {
    /// Ended calls.
    pub total: usize,
    /// Calls that failed.
    pub failed: usize,
    /// Failed over total, to 4 decimals.
    pub error_rate: f64,
    /// Sum of call durations, in milliseconds.
    pub total_duration_ms: u64,
    /// Mean call duration, to 2 decimals.
    pub average_duration_ms: f64,
    /// Longest call duration.
    pub max_duration_ms: u64,
    /// The longest call, the first of equals.
    pub slowest_tool: Option<ToolTiming>,
    /// The five longest calls, longest first, ties in call order.
    pub top_slowest: Vec<ToolTiming>,
    /// The first five failed calls.
    pub top_failures: Vec<ToolFailure>,
    /// Calls per tool.
    pub by_name: BTreeMap<String, usize>,
    /// Failed calls per tool.
    pub by_name_errors: BTreeMap<String, usize>,
}

/// One call's timing.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ToolTiming {
    /// The call's id.
    pub tool_use_id: String,
    /// The tool.
    pub tool_name: String,
    /// The start's summary.
    pub summary: String,
    /// How long the call ran, in milliseconds.
    pub duration_ms: u64,
    /// Whether the call failed.
    pub is_error: bool,
}

/// One failed call.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ToolFailure {
    /// The call's id.
    pub tool_use_id: String,
    /// The tool.
    pub tool_name: String,
    /// The start's summary.
    pub summary: String,
    /// How long the call ran, in milliseconds.
    pub duration_ms: u64,
    /// The result text, whitespace collapsed, at most 160 characters.
    pub error: String,
}

pub(super) fn tool_summary(calls: &[ToolCallRecord]) -> ToolSummary {
    let durations: Vec<u64> = calls.iter().map(|call| call.duration_ms).collect();
    let total_duration_ms = durations.iter().sum::<u64>();
    let failed: Vec<&ToolCallRecord> = calls.iter().filter(|call| call.is_error).collect();
    let mut slowest: Vec<ToolTiming> = calls.iter().map(timing).collect();
    // A stable sort keeps equal durations in call order.
    slowest.sort_by_key(|timing| std::cmp::Reverse(timing.duration_ms));
    slowest.truncate(TOP);
    let mut by_name = BTreeMap::new();
    let mut by_name_errors = BTreeMap::new();
    for call in calls {
        *by_name.entry(call.tool_name.clone()).or_default() += 1;
        if call.is_error {
            *by_name_errors.entry(call.tool_name.clone()).or_default() += 1;
        }
    }
    ToolSummary {
        total: calls.len(),
        failed: failed.len(),
        error_rate: ratio(failed.len() as u64, calls.len() as u64, 4),
        total_duration_ms,
        average_duration_ms: ratio(total_duration_ms, durations.len() as u64, 2),
        max_duration_ms: durations.iter().copied().max().unwrap_or(0),
        slowest_tool: slowest.first().cloned(),
        top_slowest: slowest,
        top_failures: failed.into_iter().take(TOP).map(failure).collect(),
        by_name,
        by_name_errors,
    }
}

fn timing(call: &ToolCallRecord) -> ToolTiming {
    ToolTiming {
        tool_use_id: call.tool_use_id.clone(),
        tool_name: call.tool_name.clone(),
        summary: call.summary.clone(),
        duration_ms: call.duration_ms,
        is_error: call.is_error,
    }
}

fn failure(call: &ToolCallRecord) -> ToolFailure {
    let text = call.result.split_whitespace().collect::<Vec<_>>().join(" ");
    let error = if text.chars().count() > 160 {
        format!("{}...", text.chars().take(157).collect::<String>())
    } else {
        text
    };
    ToolFailure {
        tool_use_id: call.tool_use_id.clone(),
        tool_name: call.tool_name.clone(),
        summary: call.summary.clone(),
        duration_ms: call.duration_ms,
        error,
    }
}
