//! `tool_retry`: run a tool again after a failure it reports as transient.
//!
//! Python's `tool_retry` retries read-scope tools on any exception. Here a
//! call is retried only when all of these hold: the agent's spec sets a
//! [`ToolRetry`]; the tool declares itself [`Tool::idempotent`]
//! (a repeat has no effect beyond its result, or the same effect twice is
//! the same); the failure is an error result whose [`ToolOutputError::code`]
//! (the class the tool reports) is in [`ToolRetry::retry_on`], or, with
//! `"timeout"` listed, a call that ran past the call timeout; and attempts
//! are left. A panic, an output that broke the tool's schema and an error
//! without a code are never retried.
//!
//! The first attempt follows the call's durable start. Each retry is made
//! durable first: a `tool_retry` event is written and flushed before the
//! backoff wait and the next attempt, so a crash can leave a call with a
//! start and attempts but no end, which a resumed run reports as
//! interrupted and never runs again (see [`crate::journal`]). A
//! non-idempotent tool is never run twice.
//!
//! [`Tool::idempotent`]: crate::Tool::idempotent
//! [`ToolOutputError::code`]: crate::ToolOutputError::code

use std::time::Duration;

use serde::{Deserialize, Serialize};

/// The class a call that ran past the call timeout reports, for
/// [`ToolRetry::retry_on`]. The timeout error itself has no code.
pub const TIMEOUT_CLASS: &str = "timeout";

/// The class retried unless the policy lists others.
pub const TRANSIENT_CLASS: &str = "transient";

/// When and how an agent's tool calls are retried. Set in
/// `AgentSpec::tool_retry`; absent, no tool is retried and the spec's
/// wire form and contract are those of a spec written before this existed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolRetry {
    /// Most attempts of one call, the first included. At least 2.
    #[serde(default = "default_max_attempts")]
    pub max_attempts: u32,
    /// Wait before the second attempt; each retry waits twice as long as
    /// the one before, up to `max_delay_ms`.
    #[serde(default = "default_base_delay_ms")]
    pub base_delay_ms: u64,
    /// Longest wait between two attempts. At least `base_delay_ms`.
    #[serde(default = "default_max_delay_ms")]
    pub max_delay_ms: u64,
    /// Error classes that are retried: the `code` of the tool's error
    /// result, or `"timeout"` for a call that ran past the call timeout.
    /// `["transient"]` when absent.
    #[serde(default = "default_retry_on")]
    pub retry_on: Vec<String>,
    /// Tools that may be retried, narrowing the ones that declare
    /// themselves idempotent: a tool that does not is never retried,
    /// whatever this lists. Absent: every idempotent tool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<String>>,
}

fn default_max_attempts() -> u32 {
    3
}

fn default_base_delay_ms() -> u64 {
    200
}

fn default_max_delay_ms() -> u64 {
    5_000
}

fn default_retry_on() -> Vec<String> {
    vec![TRANSIENT_CLASS.to_owned()]
}

impl Default for ToolRetry {
    fn default() -> Self {
        Self {
            max_attempts: default_max_attempts(),
            base_delay_ms: default_base_delay_ms(),
            max_delay_ms: default_max_delay_ms(),
            retry_on: default_retry_on(),
            tools: None,
        }
    }
}

impl ToolRetry {
    /// Why this policy cannot run, if it cannot.
    pub fn problem(&self) -> Option<&'static str> {
        if self.max_attempts < 2 {
            return Some("tool_retry.max_attempts must be at least 2");
        }
        if self.max_delay_ms < self.base_delay_ms {
            return Some("tool_retry.max_delay_ms must be at least base_delay_ms");
        }
        if self.retry_on.is_empty() || self.retry_on.iter().any(|class| class.trim().is_empty()) {
            return Some("tool_retry.retry_on needs at least one class and no blank one");
        }
        if let Some(tools) = &self.tools
            && (tools.is_empty() || tools.iter().any(|name| name.trim().is_empty()))
        {
            return Some("tool_retry.tools needs at least one name and no blank one");
        }
        None
    }

    /// Whether `tool` may be retried at all: it declares itself
    /// idempotent, and the policy's `tools` (when set) names it.
    pub(crate) fn covers(&self, tool: &str, idempotent: bool) -> bool {
        idempotent
            && self
                .tools
                .as_ref()
                .is_none_or(|names| names.iter().any(|n| n == tool))
    }

    /// Whether a failure of class `class` is retried.
    pub(crate) fn retries(&self, class: &str) -> bool {
        self.retry_on.iter().any(|listed| listed == class)
    }

    /// The wait before attempt `next` (from 2).
    pub(crate) fn delay(&self, next: u32) -> Duration {
        let doublings = next.saturating_sub(2).min(32);
        let wait = self.base_delay_ms.saturating_mul(1u64 << doublings);
        Duration::from_millis(wait.min(self.max_delay_ms))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_wait_doubles_up_to_the_cap() {
        let retry = ToolRetry {
            base_delay_ms: 100,
            max_delay_ms: 350,
            ..ToolRetry::default()
        };
        let waits: Vec<_> = (2..=6).map(|n| retry.delay(n).as_millis()).collect();
        assert_eq!(waits, [100, 200, 350, 350, 350]);
        assert_eq!(retry.delay(u32::MAX).as_millis(), 350);
    }

    #[test]
    fn a_policy_must_be_able_to_retry() {
        assert_eq!(ToolRetry::default().problem(), None);
        let bad = |edit: fn(&mut ToolRetry)| {
            let mut retry = ToolRetry::default();
            edit(&mut retry);
            retry.problem()
        };
        assert!(bad(|r| r.max_attempts = 1).is_some());
        assert!(bad(|r| r.max_delay_ms = 1).is_some());
        assert!(bad(|r| r.retry_on.clear()).is_some());
        assert!(bad(|r| r.retry_on = vec![" ".into()]).is_some());
        assert!(bad(|r| r.tools = Some(vec![])).is_some());
    }

    #[test]
    fn a_tool_must_declare_itself_idempotent_whatever_the_list_says() {
        let named = ToolRetry {
            tools: Some(vec!["Read".into()]),
            ..ToolRetry::default()
        };
        assert!(named.covers("Read", true));
        assert!(!named.covers("Read", false));
        assert!(!named.covers("Grep", true));
        assert!(ToolRetry::default().covers("Grep", true));
        assert!(!ToolRetry::default().covers("Write", false));
    }

    #[test]
    fn the_wire_form_is_strict_and_defaults_apply() {
        let read: ToolRetry = serde_json::from_str("{}").unwrap();
        assert_eq!(read, ToolRetry::default());
        assert!(serde_json::from_str::<ToolRetry>(r#"{"jitter": 0.2}"#).is_err());
        assert!(serde_json::from_str::<ToolRetry>(r#"{"max_attempts": -1}"#).is_err());
    }
}
