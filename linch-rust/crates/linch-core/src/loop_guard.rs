//! The loop guard (Python's `LoopGuard`): a run that is stuck ends
//! `failed` with a [`LoopGuardEvent`] instead of spinning until `max_turns`,
//! which is unset by default.
//!
//! Four checks, each over the run's own durable events:
//!
//! - **Repeated tool call**: the same call with the same result
//!   `max_identical_tool_calls` times among the last [`REPEAT_WINDOW`] tool
//!   results: one tool, the same input (Linch canonical JSON, so key order
//!   does not count) and the same result content. The call that reaches the
//!   limit runs; the run ends before the next provider call. A call whose
//!   result differs from every earlier identical call's made progress (a
//!   file read again after an edit) and starts a count of its own; one
//!   repeated only after many other calls (a `git status` now and then in a
//!   long run) is not a loop, which is why the count has a window. An
//!   error result is never a repeat: Linch itself asks the model to repeat
//!   some refused calls (a deferred tool: "call tool_search ... first"), and
//!   calls that keep failing are the failure streak's. The result is
//!   compared without the offload file name of a result that
//!   `ResultOffload` moved (it holds the call's id), or no offloaded result
//!   would ever repeat.
//! - **Repeated inputs**: the same call, whatever the result, errors
//!   included (Python's rule, which has no window and counts 3), `max_identical_inputs` times among the last
//!   [`INPUT_WINDOW`] tool results. It catches a loop whose result changes
//!   each time (a timestamp) or that alternates between calls. The limit is
//!   higher than the repeat's. A write or exec call that succeeds with an
//!   input not seen among the latest [`INPUT_WINDOW`] write or exec calls
//!   that succeeded (a new edit) changed the world, so the calls asked
//!   before it are new questions now and it clears the count of every other
//!   call: a test run between edits, or a file read again after an edit, is
//!   never a repeat. A write or exec call that repeats an input of that
//!   window clears nothing, so the same call (or a cycle of calls) run again
//!   and again with no new edit between still counts, and so does a
//!   read-only cycle.
//! - **Repeated failures**: `max_consecutive_failures` tool batches in a row
//!   in which every call failed. A batch with one success starts it again.
//! - **Idle turns** (Rust only): more than `max_idle_turns` turns in a row
//!   that end without a provider call, which only a `before_provider_call`
//!   `force_continue` does. Always on: a hook that skips every call would
//!   otherwise append to the session forever and never reach the budget.
//!
//! A tool that [polls](crate::Tool::polls) (`BashOutput`) asks the same
//! thing until something ends, so its running answers are neither repeats
//! nor repeated inputs: the same answer more than `max_idle_turns` times in
//! a row ends the run (waiting that long on a command that prints nothing
//! is a loop too). A poll whose call ended with [`POLL_DONE`] in its
//! metadata has settled (the command finished): reading it again is a repeat
//! like any other call, so a finished command read three times ends the run.
//!
//! Nothing new is checkpointed. `Watch::of` reads the counts back from
//! the run's `assistant` events (the calls asked for), its `tool_call_end`
//! events (whether a poll settled), its `tool_result` user events (the
//! batches' results, whose content keys the repeats) and its primary
//! `model_call_delivery` events (the turn of the last call), each durable
//! before the step that acts on it; the live run counts the same events as
//! it stages them. A crash therefore resumes with the counts it had, and
//! cannot reset them.

use std::collections::{HashMap, HashSet, VecDeque};

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::canonical::to_canonical_string;
use crate::events::kinds::{LoopGuardAction, LoopGuardEvent, LoopGuardReason, UserEventSubtype};
use crate::events::model_call::ModelCallKind;
use crate::events::{Envelope, Event};
use crate::message::ContentBlock;
use crate::permissions::permission_decision_key;

/// The key of a tool's output metadata that says a call of a tool that
/// [polls](crate::Tool::polls) found what it waits for done (the command
/// ended): a settled poll is not waiting any more, so reading it again is
/// a repeat.
pub const POLL_DONE: &str = "poll_done";

/// How many of the latest tool results the repeat count looks at.
pub const REPEAT_WINDOW: usize = 20;

/// How many of the latest tool results the identical-input count looks at.
pub const INPUT_WINDOW: usize = 50;

fn default_max_identical_tool_calls() -> u32 {
    3
}

fn default_max_identical_inputs() -> u32 {
    10
}

fn default_max_consecutive_failures() -> u32 {
    3
}

fn default_max_idle_turns() -> u32 {
    25
}

fn is_default_max_identical_inputs(limit: &u32) -> bool {
    *limit == default_max_identical_inputs()
}

/// When a run is stuck in a loop. On in every
/// [`AgentSpec`](crate::AgentSpec) with Python's defaults; 0 turns the
/// tool-call checks off, and the idle-turn bound is always on. The default
/// is left off the wire, so a spec that does not set it keeps the contract
/// of a spec written before the guard existed; so is each field added since
/// at its default.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoopGuard {
    /// Calls of one tool with the same input and the same result, not an
    /// error, among the last 20 tool results, that end the run (default 3);
    /// the one that reaches it runs first. A running poll never counts. 0
    /// never counts.
    #[serde(default = "default_max_identical_tool_calls")]
    pub max_identical_tool_calls: u32,
    /// Calls of one tool with the same input, whatever the result, among the
    /// last 50 tool results, that end the run (default 10). A tool that
    /// polls never counts. 0 never counts.
    #[serde(
        default = "default_max_identical_inputs",
        skip_serializing_if = "is_default_max_identical_inputs"
    )]
    pub max_identical_inputs: u32,
    /// Tool batches in a row whose every call failed that end the run
    /// (default 3). 0 never counts.
    #[serde(default = "default_max_consecutive_failures")]
    pub max_consecutive_failures: u32,
    /// Most turns in a row that may end without a provider call, and most
    /// identical answers in a row of a running poll (default 25, at least
    /// 1); the next one ends the run.
    #[serde(default = "default_max_idle_turns")]
    pub max_idle_turns: u32,
}

impl Default for LoopGuard {
    fn default() -> Self {
        Self {
            max_identical_tool_calls: default_max_identical_tool_calls(),
            max_identical_inputs: default_max_identical_inputs(),
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

/// A call of the last response: its permission key (tool and canonical
/// input) and its tool.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Call {
    key: String,
    tool: String,
}

/// The last `cap` keys pushed, and how many of each they hold. A slot may
/// hold no key, so a result that does not count still ages the others.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Recent {
    cap: usize,
    order: VecDeque<Option<[u8; 32]>>,
    counts: HashMap<[u8; 32], u32>,
}

impl Recent {
    fn new(cap: usize) -> Self {
        Self {
            cap,
            order: VecDeque::with_capacity(cap + 1),
            counts: HashMap::new(),
        }
    }

    /// Take `key` into the window, dropping the oldest slot past `cap`;
    /// how many slots hold `key` now (0 without one).
    fn push(&mut self, key: Option<[u8; 32]>) -> u32 {
        self.order.push_back(key);
        if let Some(key) = key {
            *self.counts.entry(key).or_default() += 1;
        }
        if self.order.len() > self.cap
            && let Some(Some(old)) = self.order.pop_front()
            && let Some(count) = self.counts.get_mut(&old)
        {
            *count -= 1;
            if *count == 0 {
                self.counts.remove(&old);
            }
        }
        key.and_then(|key| self.counts.get(&key).copied())
            .unwrap_or(0)
    }
}

impl Recent {
    /// Forget every slot that is not `key`, which keeps its count.
    fn keep_only(&mut self, key: [u8; 32]) {
        for slot in &mut self.order {
            if slot.is_some_and(|slot| slot != key) {
                *slot = None;
            }
        }
        self.counts.retain(|counted, _| *counted == key);
    }
}

/// Which limit a run reached first.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Repeat {
    /// The same call and result `count` times in the window.
    Result { tool: String, count: u32 },
    /// The same call `count` times in the window, whatever the result.
    Input { tool: String, count: u32 },
    /// The same answer of a running poll `count` times in a row.
    Poll { tool: String, count: u32 },
}

/// What the guard has counted of one run.
#[derive(Debug, Clone)]
pub(crate) struct Watch {
    guard: LoopGuard,
    /// The tools that poll.
    polls: HashSet<String>,
    /// The tools of write or exec scope, whose success changes what a later
    /// identical call would find.
    writes: HashSet<String>,
    /// The calls of the last response, by `tool_use_id`.
    pending: HashMap<String, Call>,
    /// The poll calls of the last response whose end says they settled.
    settled: HashSet<String>,
    /// The call and result of the latest tool results that can repeat.
    results: Recent,
    /// The call of the latest tool results.
    inputs: Recent,
    /// The call of the latest write or exec results that succeeded; never
    /// cleared, so an input in it is not new.
    mutations: Recent,
    /// For each running poll call, the last answer and how many times in a
    /// row it came.
    polling: HashMap<[u8; 32], ([u8; 32], u32)>,
    /// The first limit a call reached.
    repeated: Option<Repeat>,
    /// Batches in a row whose every call failed.
    failures: u32,
    /// The highest `failures` reached, so a later success does not undo a
    /// streak that already reached the limit.
    failed: Option<u32>,
    /// The turn of the run's last primary provider call.
    last_call: Option<u64>,
}

/// SHA-256 of `parts`, each length-prefixed.
fn digest(parts: &[&[u8]]) -> [u8; 32] {
    let mut hash = Sha256::new();
    for part in parts {
        hash.update(part.len().to_le_bytes());
        hash.update(part);
    }
    hash.finalize().into()
}

impl Watch {
    /// A watch of a run with nothing counted, whose `polls` tools wait for
    /// something that runs on and whose `writes` tools change state.
    pub(crate) fn new(guard: &LoopGuard, polls: HashSet<String>, writes: HashSet<String>) -> Self {
        Self {
            guard: guard.clone(),
            polls,
            writes,
            pending: HashMap::new(),
            settled: HashSet::new(),
            results: Recent::new(REPEAT_WINDOW),
            inputs: Recent::new(INPUT_WINDOW),
            mutations: Recent::new(INPUT_WINDOW),
            polling: HashMap::new(),
            repeated: None,
            failures: 0,
            failed: None,
            last_call: None,
        }
    }

    /// The counts of a run with these durable `events`.
    pub(crate) fn of(
        guard: &LoopGuard,
        polls: HashSet<String>,
        writes: HashSet<String>,
        events: &[Envelope],
    ) -> Self {
        let mut watch = Self::new(guard, polls, writes);
        for envelope in events {
            watch.see(&envelope.event);
        }
        watch
    }

    /// Whether a tool-call check is on.
    fn counting(&self) -> bool {
        self.guard.max_identical_tool_calls != 0 || self.guard.max_identical_inputs != 0
    }

    /// Count `event`, one the run stages.
    pub(crate) fn see(&mut self, event: &Event) {
        match event {
            Event::Assistant(response) => {
                self.pending.clear();
                self.settled.clear();
                if !self.counting() {
                    return;
                }
                for call in response.message.tool_uses() {
                    let Ok(key) = permission_decision_key(&call.name, &call.input) else {
                        continue;
                    };
                    let tool = call.name.clone();
                    self.pending.insert(call.id.clone(), Call { key, tool });
                }
            }
            Event::ToolCallEnd(end) if self.polls.contains(&end.tool_name) => {
                let done = (end.metadata.as_ref())
                    .and_then(|metadata| metadata.get(POLL_DONE))
                    .is_some_and(|done| done == &serde_json::Value::Bool(true));
                if done {
                    self.settled.insert(end.result.tool_use_id.clone());
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
                let settled = std::mem::take(&mut self.settled);
                if self.counting() {
                    for result in &results {
                        let call = pending.get(&result.tool_use_id);
                        let settled = settled.contains(&result.tool_use_id);
                        self.count(call, result, settled);
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

    /// Count one tool result: `call` is the call it answers, if known, and
    /// `settled` whether the call was a poll that found its command done.
    fn count(
        &mut self,
        call: Option<&Call>,
        result: &crate::message::ToolResultBlock,
        settled: bool,
    ) {
        let Some(call) = call else {
            self.results.push(None);
            self.inputs.push(None);
            return;
        };
        let polling = self.polls.contains(&call.tool);
        let input = digest(&[call.key.as_bytes()]);
        let content = result_content(call, result);
        let tool = &call.tool;

        // A write or exec call that succeeded with a new input changed
        // state, so every other call asked before it is a new question now.
        // One repeated is no change: run again and again it is the loop.
        if !result.is_error && self.writes.contains(tool) && self.mutations.push(Some(input)) == 1 {
            self.inputs.keep_only(input);
        }

        // The same call, whatever the result: not a poll's.
        let seen = self.inputs.push((!polling).then_some(input));
        let limit = self.guard.max_identical_inputs;
        if limit != 0 && seen >= limit && self.repeated.is_none() {
            self.repeated = Some(Repeat::Input {
                tool: tool.clone(),
                count: seen,
            });
        }

        // The same call and result: not an error's, nor a running poll's.
        let repeatable = !result.is_error && (!polling || settled);
        let key = content
            .as_ref()
            .filter(|_| repeatable)
            .map(|content| digest(&[call.key.as_bytes(), content.as_bytes()]));
        let seen = self.results.push(key);
        let limit = self.guard.max_identical_tool_calls;
        if limit != 0 && seen >= limit && self.repeated.is_none() {
            self.repeated = Some(Repeat::Result {
                tool: tool.clone(),
                count: seen,
            });
        }

        // A running poll that answers as it did: waiting, up to a bound.
        if polling && !settled && !result.is_error {
            let Some(content) = content else {
                return;
            };
            let answer = digest(&[content.as_bytes()]);
            let slot = self.polling.entry(input).or_insert((answer, 0));
            match slot.0 == answer {
                true => slot.1 += 1,
                false => *slot = (answer, 1),
            }
            let (count, limit) = (slot.1, self.guard.max_idle_turns);
            if self.guard.max_identical_tool_calls != 0 && count > limit && self.repeated.is_none()
            {
                self.repeated = Some(Repeat::Poll {
                    tool: tool.clone(),
                    count,
                });
            }
        }
    }

    /// The event that ends the run, when its tool calls reached a limit.
    pub(crate) fn tool_trip(&self) -> Option<LoopGuardEvent> {
        let guard = &self.guard;
        match &self.repeated {
            Some(Repeat::Result { tool, count }) => {
                return Some(stop(
                    LoopGuardReason::RepeatedToolCall,
                    format!(
                        "Tool '{tool}' called with identical inputs and the same result {count} \
                         time(s) in the last {REPEAT_WINDOW} tool results (limit: {}).",
                        guard.max_identical_tool_calls
                    ),
                ));
            }
            Some(Repeat::Input { tool, count }) => {
                return Some(stop(
                    LoopGuardReason::RepeatedToolCall,
                    format!(
                        "Tool '{tool}' called with identical inputs {count} time(s) in the last \
                         {INPUT_WINDOW} tool results, whatever the results (limit: {}).",
                        guard.max_identical_inputs
                    ),
                ));
            }
            Some(Repeat::Poll { tool, count }) => {
                return Some(stop(
                    LoopGuardReason::RepeatedToolCall,
                    format!(
                        "Tool '{tool}' polled {count} time(s) in a row with the same answer \
                         (limit: {}).",
                        guard.max_idle_turns
                    ),
                ));
            }
            None => {}
        }
        let failures = self.failed?;
        let limit = guard.max_consecutive_failures;
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

/// The canonical text of `result`'s content, with the file name of an
/// offloaded result (`{tool}_{tool_use_id}.txt`, unique to the call) taken
/// out; `None` when it cannot be written.
fn result_content(call: &Call, result: &crate::message::ToolResultBlock) -> Option<String> {
    let content = serde_json::to_value(&result.content).ok()?;
    let content = to_canonical_string(&content).ok()?;
    let name = format!("{}_{}.txt", call.tool, result.tool_use_id);
    Some(match content.contains(&name) {
        true => content.replace(&name, &format!("{}_.txt", call.tool)),
        false => content,
    })
}

fn stop(reason: LoopGuardReason, detail: String) -> LoopGuardEvent {
    LoopGuardEvent {
        reason,
        detail,
        action: LoopGuardAction::Stop,
    }
}

#[cfg(test)]
mod tests;
