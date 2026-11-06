//! The `hook` kind: one intercept-hook call, for observers.

use serde::{Deserialize, Serialize};

use crate::policy::{FailMode, HookPoint};

/// One hook call a run made, as its chain recorded it.
///
/// Stream-only: hooks are not barriers, so the record reaches the caller at
/// once, carrying the last durable seq, and is never stored. Hooks the
/// matcher filtered out were not called and leave no record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HookEvent {
    /// Point the hook ran at.
    pub point: HookPoint,
    /// The hook's policy id.
    pub policy_id: String,
    /// The hook's policy version.
    pub policy_version: u32,
    /// Tool call the hook was about, at the tool points.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_use_id: Option<String>,
    /// How the call ended.
    pub outcome: HookOutcome,
}

/// How one hook call ended.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum HookOutcome {
    /// The hook answered.
    Answered {
        /// Name of the answer (`continue`, `rewrite`, `block`, ...).
        verdict: String,
    },
    /// The hook returned an error, handled by `fail_mode`.
    Failed {
        /// The error text.
        error: String,
        /// How the failure was handled.
        fail_mode: FailMode,
    },
    /// The hook did not answer in time, handled by `fail_mode`.
    TimedOut {
        /// The timeout that elapsed, in milliseconds.
        timeout_ms: u64,
        /// How the failure was handled.
        fail_mode: FailMode,
    },
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn a_hook_event_has_a_flat_status_tag() {
        let event = HookEvent {
            point: HookPoint::Stop,
            policy_id: "grounding".into(),
            policy_version: 2,
            tool_use_id: None,
            outcome: HookOutcome::TimedOut {
                timeout_ms: 50,
                fail_mode: FailMode::Open,
            },
        };
        let value = serde_json::to_value(&event).unwrap();
        assert_eq!(
            value,
            json!({
                "point": "stop",
                "policy_id": "grounding",
                "policy_version": 2,
                "outcome": {"status": "timed_out", "timeout_ms": 50, "fail_mode": "open"},
            })
        );
        assert_eq!(serde_json::from_value::<HookEvent>(value).unwrap(), event);
        let extra = json!({"status": "answered", "verdict": "allow", "x": 1});
        assert!(serde_json::from_value::<HookOutcome>(extra).is_err());
    }
}
