//! The loop guard (Python's `LoopGuard`): a run that is stuck ends
//! `failed` with a [`LoopGuardEvent`] instead of spinning until `max_turns`,
//! which is unset by default.
//!
//! Three checks, each over the run's own durable events:
//!
//! - **Repeated tool call**: the same call with the same result
//!   `max_identical_tool_calls` times in the run: one tool, the same input
//!   (Linch canonical JSON, so key order does not count) and the same
//!   result content. The call that reaches the limit runs; the run ends
//!   before the next provider call. A call whose result differs from every
//!   earlier identical call's made progress (a file read again after an
//!   edit) and starts a count of its own. An error result is never a
//!   repeat: Linch itself asks the model to repeat some refused calls (a
//!   deferred tool: "call tool_search ... first"), and calls that keep
//!   failing are the failure streak's. Nor is a call of a tool that
//!   [polls](crate::Tool::polls) (`BashOutput`): waiting on a command that
//!   runs on asks the same thing until it ends. Python counts identical
//!   inputs whatever the result, failed and refused calls included.
//! - **Repeated failures**: `max_consecutive_failures` tool batches in a row
//!   in which every call failed. A batch with one success starts it again.
//! - **Idle turns** (Rust only): more than `max_idle_turns` turns in a row
//!   that end without a provider call, which only a `before_provider_call`
//!   `force_continue` does. Always on: a hook that skips every call would
//!   otherwise append to the session forever and never reach the budget.
//!
//! Nothing new is checkpointed. `Watch::of` reads the counts back from
//! the run's `assistant` events (the calls asked for), its `tool_result`
//! user events (the batches' results, whose content keys the repeats) and
//! its primary `model_call_delivery` events (the turn of the last call),
//! each durable before the step that acts on it; the live run counts the
//! same events as it stages them. A crash therefore resumes with the
//! counts it had, and cannot reset them.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::canonical::to_canonical_string;
use crate::events::kinds::{LoopGuardAction, LoopGuardEvent, LoopGuardReason, UserEventSubtype};
use crate::events::model_call::ModelCallKind;
use crate::events::{Envelope, Event};
use crate::message::ContentBlock;
use crate::permissions::permission_decision_key;

fn default_max_identical_tool_calls() -> u32 {
    3
}

fn default_max_consecutive_failures() -> u32 {
    3
}

fn default_max_idle_turns() -> u32 {
    25
}

/// When a run is stuck in a loop. On in every
/// [`AgentSpec`](crate::AgentSpec) with Python's defaults; 0 turns the
/// tool-call checks off, and the idle-turn bound is always on. The default
/// is left off the wire, so a spec that does not set it keeps the contract
/// of a spec written before the guard existed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoopGuard {
    /// Calls of one tool with the same input and the same result, not an
    /// error, that end the run (default 3); the one that reaches it runs
    /// first. A tool that polls never counts. 0 never counts.
    #[serde(default = "default_max_identical_tool_calls")]
    pub max_identical_tool_calls: u32,
    /// Tool batches in a row whose every call failed that end the run
    /// (default 3). 0 never counts.
    #[serde(default = "default_max_consecutive_failures")]
    pub max_consecutive_failures: u32,
    /// Most turns in a row that may end without a provider call (default
    /// 25, at least 1); the next one ends the run.
    #[serde(default = "default_max_idle_turns")]
    pub max_idle_turns: u32,
}

impl Default for LoopGuard {
    fn default() -> Self {
        Self {
            max_identical_tool_calls: default_max_identical_tool_calls(),
            max_consecutive_failures: default_max_consecutive_failures(),
            max_idle_turns: default_max_idle_turns(),
        }
    }
}

impl LoopGuard {
    /// Whether this is the default guard, which a spec leaves off the wire.
    pub fn is_default(&self) -> bool {
        *self == Self::default()
    }

    /// Why this guard cannot run, if it cannot.
    pub(crate) fn problem(&self) -> Option<&'static str> {
        (self.max_idle_turns == 0).then_some("loop_guard.max_idle_turns must be at least 1")
    }
}

/// What the guard has counted of one run.
#[derive(Debug, Clone)]
pub(crate) struct Watch {
    guard: LoopGuard,
    /// The tools that poll, whose calls are never repeats.
    polls: HashSet<String>,
    /// The calls of the last response, by `tool_use_id`: their tool and
    /// canonical input, and the tool.
    pending: HashMap<String, (String, String)>,
    /// Calls per tool, canonical input and result content that did not
    /// fail, keyed by the hash of the three.
    calls: HashMap<[u8; 32], u32>,
    /// The first call that reached `max_identical_tool_calls`: its tool and
    /// count.
    repeated: Option<(String, u32)>,
    /// Batches in a row whose every call failed.
    failures: u32,
    /// The highest `failures` reached, so a later success does not undo a
    /// streak that already reached the limit.
    failed: Option<u32>,
    /// The turn of the run's last primary provider call.
    last_call: Option<u64>,
}

impl Watch {
    /// A watch of a run with nothing counted, whose `polls` tools are
    /// never repeats.
    pub(crate) fn new(guard: &LoopGuard, polls: HashSet<String>) -> Self {
        Self {
            guard: guard.clone(),
            polls,
            pending: HashMap::new(),
            calls: HashMap::new(),
            repeated: None,
            failures: 0,
            failed: None,
            last_call: None,
        }
    }

    /// The counts of a run with these durable `events`.
    pub(crate) fn of(guard: &LoopGuard, polls: HashSet<String>, events: &[Envelope]) -> Self {
        let mut watch = Self::new(guard, polls);
        for envelope in events {
            watch.see(&envelope.event);
        }
        watch
    }

    /// Count `event`, one the run stages.
    pub(crate) fn see(&mut self, event: &Event) {
        match event {
            Event::Assistant(response) => {
                self.pending.clear();
                if self.guard.max_identical_tool_calls == 0 {
                    return;
                }
                for call in response.message.tool_uses() {
                    if self.polls.contains(&call.name) {
                        continue;
                    }
                    let Ok(key) = permission_decision_key(&call.name, &call.input) else {
                        continue;
                    };
                    self.pending
                        .insert(call.id.clone(), (key, call.name.clone()));
                }
            }
            Event::User(user) if user.subtype == UserEventSubtype::ToolResult => {
                let results: Vec<_> = (user.message.content.iter())
                    .filter_map(|block| match block {
                        ContentBlock::ToolResult(result) => Some(result),
                        _ => None,
                    })
                    .collect();
                let pending = std::mem::take(&mut self.pending);
                for result in results.iter().filter(|result| !result.is_error) {
                    let Some((key, tool)) = pending.get(&result.tool_use_id) else {
                        continue;
                    };
                    let content = serde_json::to_value(&result.content).ok();
                    let Some(content) = content.and_then(|c| to_canonical_string(&c).ok()) else {
                        continue;
                    };
                    let mut hash = Sha256::new();
                    hash.update(key.len().to_le_bytes());
                    hash.update(key);
                    hash.update(content);
                    let count = self.calls.entry(hash.finalize().into()).or_default();
                    *count += 1;
                    let limit = self.guard.max_identical_tool_calls;
                    if *count >= limit && self.repeated.is_none() {
                        self.repeated = Some((tool.clone(), *count));
                    }
                }
                let limit = self.guard.max_consecutive_failures;
                if results.is_empty() || limit == 0 {
                    return;
                }
                match results.iter().all(|result| result.is_error) {
                    true => self.failures += 1,
                    false => self.failures = 0,
                }
                if self.failures >= limit && self.failed.is_none() {
                    self.failed = Some(self.failures);
                }
            }
            Event::ModelCallDelivery(delivery) if delivery.kind == ModelCallKind::Primary => {
                self.last_call = delivery.turn_index.or(self.last_call);
            }
            _ => {}
        }
    }

    /// The event that ends the run, when its tool calls reached a limit.
    pub(crate) fn tool_trip(&self) -> Option<LoopGuardEvent> {
        if let Some((tool, count)) = &self.repeated {
            let limit = self.guard.max_identical_tool_calls;
            return Some(stop(
                LoopGuardReason::RepeatedToolCall,
                format!(
                    "Tool '{tool}' called with identical inputs and the same result {count} \
                     time(s) (limit: {limit})."
                ),
            ));
        }
        let failures = self.failed?;
        let limit = self.guard.max_consecutive_failures;
        Some(stop(
            LoopGuardReason::RepeatedFailures,
            format!("All tool calls failed for {failures} consecutive batch(es) (limit: {limit})."),
        ))
    }

    /// The event that ends the run, when turn `turn` would be one more turn
    /// without a provider call than `max_idle_turns` allows.
    pub(crate) fn idle_trip(&self, turn: u32) -> Option<LoopGuardEvent> {
        let since = self.last_call.map_or(0, |call| call + 1);
        let idle = u64::from(turn).saturating_sub(since) + 1;
        let limit = self.guard.max_idle_turns;
        (idle > u64::from(limit)).then(|| {
            stop(
                LoopGuardReason::IdleTurns,
                format!("{idle} turns in a row ended without a provider call (limit: {limit})."),
            )
        })
    }
}

fn stop(reason: LoopGuardReason, detail: String) -> LoopGuardEvent {
    LoopGuardEvent {
        reason,
        detail,
        action: LoopGuardAction::Stop,
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::events::WIRE_VERSION;
    use crate::events::kinds::{AssistantEvent, UserEvent};
    use crate::events::model_call::ModelCallDeliveryEvent;
    use crate::ids::RunId;
    use crate::json::JsonObject;
    use crate::message::{Message, Role, ToolResultBlock, ToolUseBlock};
    use crate::run::StopReason;

    /// A batch of `calls`, each answered with a result that failed or not.
    fn batch(watch: &mut Watch, calls: &[(&str, serde_json::Value, bool)]) {
        let asked: Vec<_> = calls.iter().map(|(n, i, _)| (*n, i.clone())).collect();
        watch.see(&calling(&asked));
        let failed: Vec<_> = calls.iter().map(|(_, _, failed)| *failed).collect();
        watch.see(&results(&failed));
    }

    fn calling(calls: &[(&str, serde_json::Value)]) -> Event {
        let blocks = calls.iter().enumerate().map(|(at, (name, input))| {
            ContentBlock::ToolUse(ToolUseBlock {
                id: format!("c{at}"),
                name: (*name).into(),
                input: input.as_object().cloned().unwrap_or_else(JsonObject::new),
            })
        });
        Event::Assistant(AssistantEvent {
            message: Message::new(Role::Assistant, blocks.collect()),
            stop_reason: StopReason::ToolUse,
        })
    }

    fn results(failed: &[bool]) -> Event {
        let answers: Vec<_> = failed.iter().map(|failed| ("r", *failed)).collect();
        answered(&answers)
    }

    /// Results with these texts, failed or not.
    fn answered(answers: &[(&str, bool)]) -> Event {
        let blocks = answers.iter().enumerate().map(|(at, (text, failed))| {
            ContentBlock::ToolResult(ToolResultBlock::text(format!("c{at}"), *text, *failed))
        });
        Event::User(UserEvent {
            message: Message::new(Role::User, blocks.collect()),
            subtype: UserEventSubtype::ToolResult,
        })
    }

    fn delivery(kind: ModelCallKind, turn: u64) -> Event {
        Event::ModelCallDelivery(ModelCallDeliveryEvent {
            run_id: RunId::new("r"),
            logical_call_id: "l".into(),
            delivery_attempt_id: "d".into(),
            kind,
            turn_index: Some(turn),
            requested_model: "m".into(),
        })
    }

    #[test]
    fn identical_calls_trip_at_the_limit_whatever_the_key_order() {
        let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
        let (read, other) = (json!({"a": 1, "b": 2}), json!({"b": 2, "a": 1}));
        batch(&mut watch, &[("Read", read.clone(), false)]);
        batch(
            &mut watch,
            &[("Read", other, false), ("Read", json!({"a": 3}), false)],
        );
        assert!(watch.tool_trip().is_none());
        batch(&mut watch, &[("Grep", read.clone(), false)]);
        assert!(watch.tool_trip().is_none(), "another tool is another call");
        batch(&mut watch, &[("Read", read.clone(), true)]);
        assert!(watch.tool_trip().is_none(), "a failed call does not count");
        watch.see(&calling(&[("Read", read.clone())]));
        assert!(watch.tool_trip().is_none(), "a call counts with its result");
        watch.see(&results(&[false]));
        let trip = watch.tool_trip().unwrap();
        assert_eq!(trip.reason, LoopGuardReason::RepeatedToolCall);
        assert_eq!(trip.action, LoopGuardAction::Stop);
        assert!(trip.detail.contains("'Read'") && trip.detail.contains("3 time(s)"));
    }

    #[test]
    fn a_call_is_a_repeat_only_with_the_same_result() {
        let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
        let read = json!({"path": "a"});
        for text in ["v1", "v1", "v2", "v2", "v3"] {
            watch.see(&calling(&[("Read", read.clone())]));
            watch.see(&answered(&[(text, false)]));
        }
        assert!(watch.tool_trip().is_none(), "each content twice at most");
        watch.see(&calling(&[("Read", read.clone())]));
        watch.see(&answered(&[("v1", false)]));
        let trip = watch.tool_trip().unwrap();
        assert!(
            trip.detail.contains("same result 3 time(s)"),
            "{}",
            trip.detail
        );
    }

    #[test]
    fn a_poll_is_never_a_repeat_but_its_failures_count() {
        let polls = HashSet::from(["BashOutput".to_owned()]);
        let mut watch = Watch::new(&LoopGuard::default(), polls);
        let poll = json!({"process_id": "bash_1"});
        for _ in 0..5 {
            batch(&mut watch, &[("BashOutput", poll.clone(), false)]);
        }
        assert!(watch.tool_trip().is_none());
        for _ in 0..3 {
            batch(&mut watch, &[("BashOutput", poll.clone(), true)]);
        }
        assert_eq!(
            watch.tool_trip().unwrap().reason,
            LoopGuardReason::RepeatedFailures
        );
    }

    #[test]
    fn a_failure_streak_trips_and_a_success_resets_it() {
        let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
        for batch in [&[true][..], &[true, true], &[false, true], &[true], &[true]] {
            watch.see(&results(batch));
        }
        assert!(watch.tool_trip().is_none());
        watch.see(&results(&[true]));
        assert_eq!(
            watch.tool_trip().unwrap().reason,
            LoopGuardReason::RepeatedFailures
        );
        watch.see(&results(&[false]));
        assert!(watch.tool_trip().is_some(), "a reached limit stays reached");
    }

    #[test]
    fn zero_turns_a_tool_check_off() {
        let off = LoopGuard {
            max_identical_tool_calls: 0,
            max_consecutive_failures: 0,
            ..LoopGuard::default()
        };
        let mut watch = Watch::new(&off, HashSet::new());
        for failed in [true, false, true, true, false, true, true] {
            batch(&mut watch, &[("Read", json!({}), failed)]);
        }
        assert!(watch.tool_trip().is_none());
    }

    #[test]
    fn idle_turns_count_from_the_last_primary_call() {
        let guard = LoopGuard {
            max_idle_turns: 2,
            ..LoopGuard::default()
        };
        let mut watch = Watch::new(&guard, HashSet::new());
        assert!(watch.idle_trip(0).is_none());
        assert!(watch.idle_trip(1).is_none());
        assert_eq!(
            watch.idle_trip(2).unwrap().reason,
            LoopGuardReason::IdleTurns
        );
        watch.see(&delivery(ModelCallKind::Primary, 4));
        watch.see(&delivery(ModelCallKind::Compaction, 6));
        assert!(watch.idle_trip(6).is_none(), "turns 5 and 6 skip");
        assert!(
            watch.idle_trip(7).is_some(),
            "a summary call is not the turn's"
        );
    }

    #[test]
    fn the_counts_read_back_from_the_log_are_the_live_ones() {
        let events = [
            calling(&[("Read", json!({}))]),
            results(&[false]),
            delivery(ModelCallKind::Primary, 0),
            calling(&[("Read", json!({})), ("Grep", json!({}))]),
            results(&[true, true]),
            calling(&[("Read", json!({}))]),
        ];
        let mut live = Watch::new(&LoopGuard::default(), HashSet::new());
        for event in &events {
            live.see(event);
        }
        let envelopes: Vec<Envelope> = (1..)
            .zip(events)
            .map(|(seq, event)| Envelope {
                v: WIRE_VERSION,
                seq,
                run_id: RunId::new("r"),
                ts_ms: 0,
                ignorable: false,
                event,
            })
            .collect();
        let read = Watch::of(&LoopGuard::default(), HashSet::new(), &envelopes);
        assert_eq!(
            (read.pending, read.calls, read.failures, read.last_call),
            (live.pending, live.calls, live.failures, live.last_call)
        );
    }

    #[test]
    fn the_idle_bound_cannot_be_turned_off() {
        let off = LoopGuard {
            max_idle_turns: 0,
            ..LoopGuard::default()
        };
        assert!(off.problem().is_some());
        assert!(LoopGuard::default().problem().is_none());
    }
}
