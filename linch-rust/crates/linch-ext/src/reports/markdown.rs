//! [`RunReport::to_markdown`].

use std::fmt::Display;

use super::{RunReport, wire_name};

impl RunReport {
    /// The report as Markdown: the counts, the summary, the slowest and
    /// failing tools, the long-run signals, the final result and one row
    /// per tool call. An absent value reads `none`.
    pub fn to_markdown(&self) -> String {
        let summary = &self.summary;
        let tools = &summary.tools;
        let context = &summary.context;
        let run_id = if self.run_id.is_empty() {
            "<unknown>"
        } else {
            &self.run_id
        };
        let mut lines = vec![
            format!("# Linch Run Report: {run_id}"),
            String::new(),
            format!("- status: {}", self.status),
            format!("- phase: {}", self.phase.as_deref().unwrap_or("<none>")),
            format!("- events: {}", self.event_count),
            format!(
                "- tool calls: {} ({} failed)",
                self.tool_calls.len(),
                self.failed_tool_calls()
            ),
            format!("- permission requests: {}", self.permission_requests.len()),
            format!("- context builds: {}", self.context_builds.len()),
            format!("- loop guard events: {}", self.loop_guards.len()),
            format!("- errors: {}", self.errors.len()),
            format!("- total_tokens: {}", summary.usage.total_tokens),
            String::new(),
            "## Summary".to_owned(),
            format!(
                "- tool duration ms: total={} avg={} max={}",
                tools.total_duration_ms, tools.average_duration_ms, tools.max_duration_ms
            ),
            format!("- tool error rate: {}", tools.error_rate),
            format!("- cache read ratio: {}", summary.usage.cache_read_ratio),
            format!(
                "- prompt cache advisories: {} (tool changes={})",
                summary.prompt_cache.advisory_count, summary.prompt_cache.tool_selection_changes
            ),
            format!(
                "- max context utilization: {}",
                or_none(context.max_utilization)
            ),
            format!("- context pressure: {}", context.pressure),
        ];
        if let Some(slowest) = &tools.slowest_tool {
            lines.push(format!(
                "- slowest tool: {} ({}ms)",
                slowest.tool_name, slowest.duration_ms
            ));
        }
        if !tools.top_slowest.is_empty() {
            lines.extend(table(
                "Top Slow Tools",
                "| Tool | Summary | Error | Duration ms |\n|---|---|---:|---:|",
            ));
            for call in &tools.top_slowest {
                let row = [&call.tool_name, &call.summary].map(|text| cell(text));
                lines.push(format!(
                    "| {} | {} | {} | {} |",
                    row[0], row[1], call.is_error, call.duration_ms
                ));
            }
        }
        if !tools.top_failures.is_empty() {
            lines.extend(table(
                "Failing Tools",
                "| Tool | Summary | Duration ms | Detail |\n|---|---|---:|---|",
            ));
            for call in &tools.top_failures {
                let row = [&call.tool_name, &call.summary, &call.error].map(|text| cell(text));
                lines.push(format!(
                    "| {} | {} | {} | {} |",
                    row[0], row[1], call.duration_ms, row[2]
                ));
            }
        }
        let long_context = &self.long_run.context;
        lines.extend([
            String::new(),
            "## Long-Run Signals".to_owned(),
            format!("- context trimmed builds: {}", long_context.trimmed_builds),
            format!(
                "- max context tokens: {}",
                or_none(long_context.max_used_tokens)
            ),
        ]);
        if let Some(result) = &self.final_result {
            lines.extend([
                String::new(),
                "## Final".to_owned(),
                format!("- subtype: {}", wire_name(&result.subtype)),
                format!("- stop_reason: {}", wire_name(&result.stop_reason)),
            ]);
            if let Some(text) = result.final_text.as_deref().filter(|text| !text.is_empty()) {
                lines.extend([String::new(), text.to_owned()]);
            }
        }
        if !self.tool_calls.is_empty() {
            lines.extend(table(
                "Tool Calls",
                "| Tool | Summary | Error | Duration ms |\n|---|---|---:|---:|",
            ));
            for call in &self.tool_calls {
                let row = [&call.tool_name, &call.summary].map(|text| cell(text));
                lines.push(format!(
                    "| {} | {} | {} | {} |",
                    row[0], row[1], call.is_error, call.duration_ms
                ));
            }
        }
        lines.join("\n")
    }
}

/// A section heading followed by a table header.
fn table(title: &str, header: &str) -> [String; 4] {
    [
        String::new(),
        format!("## {title}"),
        String::new(),
        header.to_owned(),
    ]
}

fn or_none(value: Option<impl Display>) -> String {
    value.map_or_else(|| "none".to_owned(), |value| value.to_string())
}

/// One table cell: pipes escaped, newlines as spaces.
fn cell(value: &str) -> String {
    value.replace('|', "\\|").replace('\n', " ")
}
