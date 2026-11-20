//! The reliability policy (`spec.policy`): which tools must succeed before
//! another may run, and what a run must have done before it may end.
//!
//! This is the run-level counterpart of [`crate::reliability`], which
//! retries a failed provider call; the two share only a word. A policy has
//! two kinds of rule, both over the run's own durable events:
//!
//! - **Prerequisites** ([`Prerequisite`]): a call of `tool` is answered
//!   with an error result naming what to run first, and does not run, until
//!   every tool it `requires` has succeeded in an earlier tool batch of the
//!   run. Calls in one batch never unlock each other. The scheduler decides
//!   it before the call's `pre_tool_use` hooks and before permission, so a
//!   refused call asks nobody. **It is an ordering aid for the model, not a
//!   security boundary**: it guards the run's calls only (a subagent has
//!   its own run, which takes the same prerequisites over its own calls),
//!   and a tool that can start other work can do what a refused one would.
//!   Keep permissions and the sandbox for security.
//! - **Completion gates** ([`CompletionGate`]): the run may end with a
//!   final answer (text, or the final tool's input) only when every gate
//!   holds: each of its `succeeded` tools has succeeded `min_calls` times
//!   in the run, and its `check` (a verification [`Check`]) passes on the
//!   answer. A failing answer goes back to the model as one correction
//!   turn, up to `max_corrections` in the run; the next failure ends the
//!   run `failed` with a `PolicyGateError` and a `policy` event whose
//!   action is `stop`. Unlike a `verification` verifier, which accepts the
//!   answer once its retries are spent, a gate never lets the answer
//!   through. A correction is a turn like any other: `max_turns` and the
//!   budget bound it, and the loop guard does not count it as idle or
//!   repeated (the turn had a provider call and ran no tool).
//!
//! A call counts as a success when its result is not an error, after the
//! hooks, and it did not start a background run (`run_in_background`),
//! which only acknowledges. Counted by tool name and rendered result
//! alone, a host tool of that name, a `pre_tool_use` answer, a
//! `post_tool_use` replacement or a cache hit counts too: the policy binds
//! the model's call order, not whoever supplies the tools. An interrupted call's placeholder is an
//! error. The final tool and tools the agent does not have cannot be
//! named: the first never runs, so it could never succeed.
//!
//! Nothing is checkpointed. `Gates::of` reads the state back from the
//! run's `assistant` events (the calls asked for), its `tool_result` user
//! events (their results, one per batch, durable together) and its
//! `policy` events (the refusals made and the corrections spent); the live
//! run counts the same events as it stages them. A crash therefore resumes
//! with the state it had, and no resume can skip a gate or spend a
//! correction twice.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::agent::BuildError;
use crate::events::kinds::{PolicyAction, PolicyEvent, PolicyRule, UserEventSubtype};
use crate::events::{Envelope, Event};
use crate::json::{JsonObject, JsonValue};
use crate::message::ContentBlock;
use crate::provider::ToolChoice;
use crate::verification::{Check, OnFail, VerificationSpec, Verifier, Verifiers};

/// Corrections a policy allows unless it says otherwise.
pub const DEFAULT_MAX_CORRECTIONS: u32 = 2;

fn default_min_calls() -> u32 {
    1
}

fn is_one(value: &u32) -> bool {
    *value == 1
}

/// What an agent's runs must do in order, as data. Unset in the spec means
/// off: nothing is checked, and the spec's wire form and contract digest
/// are those of a spec written before policies existed. Set, even empty,
/// it is part of the run contract.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReliabilityPolicy {
    /// Tools that may only run after others succeeded. At most one entry
    /// per tool; the entries may not form a cycle.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub prerequisites: Vec<Prerequisite>,
    /// What a final answer must satisfy, evaluated together: one
    /// correction turn answers every gate that fails at once.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gates: Vec<CompletionGate>,
    /// How many correction turns the gates may spend in one run, across
    /// all of them; 0 ends the run at the first failure. `None` (absent on
    /// the wire) is [`DEFAULT_MAX_CORRECTIONS`] for a run, and no cap of
    /// its own when a tenant's policy is merged into an operator's.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_corrections: Option<u32>,
}

/// `tool` may only run after every tool in `requires` has succeeded.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prerequisite {
    /// The tool that is held back.
    pub tool: String,
    /// The tools that must have succeeded first; at least one.
    pub requires: Vec<String>,
}

/// One condition on the final answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompletionGate {
    /// Names the gate in `policy` events; unique.
    pub name: String,
    /// Tools that must have succeeded `min_calls` times in the run.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub succeeded: Vec<String>,
    /// How many successful calls each of `succeeded` needs (default 1).
    #[serde(default = "default_min_calls", skip_serializing_if = "is_one")]
    pub min_calls: u32,
    /// A check the answer's text must pass, one of the verification
    /// checks (see [`crate::verification`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub check: Option<Check>,
    /// What the model is told when the gate fails; a sentence naming the
    /// missing calls or the check when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feedback: Option<String>,
}

/// Why a [`ReliabilityPolicy`] cannot run on an agent, and whose text it
/// can name.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PolicyProblem {
    /// A defect in the text of one rule or gate, found without the agent
    /// or the other rules: only the author of that text can have caused it.
    Own(String),
    /// A problem of the agent's tools or of rules read together, which a
    /// policy merged from two authors can have without either being wrong
    /// alone.
    Joint(String),
}

impl From<PolicyProblem> for BuildError {
    fn from(problem: PolicyProblem) -> Self {
        match problem {
            PolicyProblem::Own(text) => BuildError::Invalid(text),
            PolicyProblem::Joint(text) => BuildError::Policy(text),
        }
    }
}

impl ReliabilityPolicy {
    /// The corrections a run may spend: the policy's, else the default.
    pub fn corrections(&self) -> u32 {
        self.max_corrections.unwrap_or(DEFAULT_MAX_CORRECTIONS)
    }

    /// Why a gate's `check` cannot be used, if it cannot: a pattern that
    /// does not compile, or a keyword list that is empty or holds a blank
    /// keyword. The reason echoes the pattern.
    pub fn compile_problem(&self) -> Option<String> {
        Rules::new(self.clone()).err()
    }

    /// Why this policy cannot run on an agent, if it cannot.
    ///
    /// A defect in the text of one rule or gate, whatever the agent and the
    /// other rules, is [`PolicyProblem::Own`]; one that depends on the
    /// agent's tools or on how rules combine is [`PolicyProblem::Joint`].
    ///
    /// Args:
    ///     offers: Whether the agent has the named tool.
    ///     final_tool: The agent's final tool, which no rule may name.
    pub(crate) fn problem(
        &self,
        offers: impl Fn(&str) -> bool,
        final_tool: Option<&str>,
    ) -> Option<PolicyProblem> {
        let (own, joint) = (PolicyProblem::Own, PolicyProblem::Joint);
        let known = |tool: &str, what: &str| -> Option<PolicyProblem> {
            if tool.trim().is_empty() {
                return Some(joint(format!("policy: {what} names a blank tool")));
            }
            if final_tool == Some(tool) {
                return Some(joint(format!(
                    "policy: {what} names the final tool {tool:?}, whose call ends the run \
                     instead of running"
                )));
            }
            (!offers(tool)).then(|| {
                joint(format!(
                    "policy: {what} names {tool:?}, which is not a tool the agent has"
                ))
            })
        };
        let mut seen = HashSet::new();
        for rule in &self.prerequisites {
            if let Some(problem) = known(&rule.tool, "a prerequisite") {
                return Some(problem);
            }
            if !seen.insert(rule.tool.as_str()) {
                return Some(own(format!(
                    "policy: two prerequisites name {:?}; list its requirements in one",
                    rule.tool
                )));
            }
            if rule.requires.is_empty() {
                return Some(own(format!(
                    "policy: the prerequisite of {:?} requires no tool",
                    rule.tool
                )));
            }
            let mut required = HashSet::new();
            for tool in &rule.requires {
                if let Some(problem) = known(tool, "a prerequisite") {
                    return Some(problem);
                }
                if tool == &rule.tool {
                    return Some(own(format!("policy: {tool:?} cannot require itself")));
                }
                if !required.insert(tool.as_str()) {
                    return Some(own(format!(
                        "policy: the prerequisite of {:?} lists {tool:?} twice",
                        rule.tool
                    )));
                }
            }
        }
        if let Some(tool) = self.cycle() {
            return Some(joint(format!(
                "policy: the prerequisites of {tool:?} lead back to {tool:?}, so it could never run"
            )));
        }
        let mut names = HashSet::new();
        for gate in &self.gates {
            let name = gate.name.trim();
            if name.is_empty() {
                return Some(own("policy: a gate's name must not be blank".into()));
            }
            if !names.insert(name) {
                return Some(own(format!("policy: two gates are named {name:?}")));
            }
            if gate.succeeded.is_empty() && gate.check.is_none() {
                return Some(own(format!(
                    "policy: gate {name:?} asks for nothing: set succeeded or check"
                )));
            }
            if gate.min_calls == 0 {
                return Some(own(format!(
                    "policy: gate {name:?} needs min_calls of at least 1"
                )));
            }
            let mut required = HashSet::new();
            for tool in &gate.succeeded {
                if let Some(problem) = known(tool, &format!("gate {name:?}")) {
                    return Some(problem);
                }
                if !required.insert(tool.as_str()) {
                    return Some(own(format!("policy: gate {name:?} lists {tool:?} twice")));
                }
            }
        }
        None
    }

    /// Why `choice`, a tool choice that holds every turn, would keep this
    /// policy from ever being met, if it would.
    ///
    /// Args:
    ///     choice: The tool choice of the agent or of one run.
    ///     final_tool: The run's final tool, which lets a forced run end.
    pub(crate) fn choice_problem(
        &self,
        choice: &ToolChoice,
        final_tool: Option<&str>,
    ) -> Option<String> {
        // Whether a rule needs tools called: a held-back tool, or a gate
        // that counts successes. Check-only gates need an answer, not a call.
        let calls =
            !self.prerequisites.is_empty() || (self.gates.iter()).any(|g| !g.succeeded.is_empty());
        let rules = calls || !self.gates.is_empty();
        let (what, unmet) = match choice {
            ToolChoice::Auto {} => return None,
            ToolChoice::Disabled {} => ("none", calls),
            // The model may call any tool, and the final one ends the run.
            ToolChoice::Required {} => ("required", rules && final_tool.is_none()),
            ToolChoice::Tool { name } if final_tool == Some(name.as_str()) => {
                ("the final tool", calls)
            }
            ToolChoice::Tool { .. } => ("a named tool", rules),
        };
        unmet.then(|| {
            format!(
                "policy: tool_choice {what} holds every turn, so the policy's prerequisites and \
                 gates could not be met; use auto, or the final tool to end the run"
            )
        })
    }

    /// A tool whose requirements lead back to itself, if there is one.
    fn cycle(&self) -> Option<&str> {
        let edges: HashMap<&str, &[String]> = (self.prerequisites.iter())
            .map(|rule| (rule.tool.as_str(), rule.requires.as_slice()))
            .collect();
        // 0 unseen, 1 on the path, 2 done.
        let mut state: HashMap<&str, u8> = HashMap::new();
        for start in edges.keys() {
            let mut stack = vec![(*start, 0usize)];
            while let Some((tool, next)) = stack.pop() {
                if next == 0 {
                    match state.get(tool).copied().unwrap_or(0) {
                        1 => return Some(tool),
                        2 => continue,
                        _ => {
                            state.insert(tool, 1);
                        }
                    }
                }
                let requires = edges.get(tool).copied().unwrap_or(&[]);
                match requires.get(next) {
                    Some(required) => {
                        stack.push((tool, next + 1));
                        stack.push((required.as_str(), 0));
                    }
                    None => {
                        state.insert(tool, 2);
                    }
                }
            }
        }
        None
    }

    /// The policy a subagent's runs take: the prerequisites, which hold
    /// over its own calls, and none of the gates, which a child answering
    /// its parent in text could not always meet with its own tools.
    pub(crate) fn for_child(&self) -> Self {
        Self {
            prerequisites: self.prerequisites.clone(),
            gates: Vec::new(),
            max_corrections: self.max_corrections,
        }
    }
}

/// A [`ReliabilityPolicy`] compiled once, for every run of an agent.
#[derive(Debug)]
pub(crate) struct Rules {
    policy: ReliabilityPolicy,
    /// What each held-back tool requires.
    requires: HashMap<String, Vec<String>>,
    /// The tools any rule counts successes of.
    referenced: HashSet<String>,
    /// Each gate's check, in the gates' order.
    checks: Vec<Option<Verifiers>>,
}

impl Rules {
    /// Compile `policy`.
    ///
    /// Raises:
    ///     String: Why a gate's check cannot be used (a pattern that does
    ///         not compile, a keyword list that is empty or holds a blank
    ///         keyword).
    pub(crate) fn new(policy: ReliabilityPolicy) -> Result<Self, String> {
        let mut requires = HashMap::new();
        let mut referenced = HashSet::new();
        for rule in &policy.prerequisites {
            requires.insert(rule.tool.clone(), rule.requires.clone());
            referenced.extend(rule.requires.iter().cloned());
        }
        let mut checks = Vec::with_capacity(policy.gates.len());
        for gate in &policy.gates {
            referenced.extend(gate.succeeded.iter().cloned());
            let compiled = match &gate.check {
                None => None,
                Some(check) => {
                    let verifier = Verifier {
                        name: gate.name.clone(),
                        check: check.clone(),
                        feedback: gate.feedback.clone(),
                        on_fail: OnFail::Retry,
                    };
                    let spec = VerificationSpec {
                        max_retries: 0,
                        verifiers: vec![verifier],
                    };
                    let verifiers = Verifiers::new(&spec);
                    Some(verifiers.map_err(|e| format!("policy: gate {:?}: {e}", gate.name))?)
                }
            };
            checks.push(compiled);
        }
        Ok(Self {
            policy,
            requires,
            referenced,
            checks,
        })
    }
}

/// A call refused for tools that have not succeeded yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Refusal {
    /// The tools still to succeed.
    pub(crate) missing: Vec<String>,
    /// The error result the model gets.
    pub(crate) message: String,
}

/// A final answer the gates do not let through.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Failure {
    /// The gates that fail, in order.
    pub(crate) gates: Vec<String>,
    /// The tools they still need to see succeed.
    pub(crate) missing: Vec<String>,
    /// What the model is told.
    pub(crate) feedback: String,
}

/// What the policy has counted of one run.
#[derive(Debug, Clone)]
pub(crate) struct Gates {
    rules: Arc<Rules>,
    /// The calls of the last response by `tool_use_id`: their tool, and
    /// whether the call started a background run.
    pending: HashMap<String, (String, bool)>,
    /// Successful calls of each tool a rule counts.
    successes: HashMap<String, u32>,
    /// Responses seen so far: the turn a call belongs to.
    responses: u32,
    /// The calls already refused, by response and `tool_use_id` (a provider
    /// may reuse an id on a later turn), so that a re-plan of their batch
    /// after a crash does not report them twice.
    refused: HashSet<(u32, String)>,
    /// Corrections spent.
    corrections: u32,
}

impl Gates {
    /// The state of a run with nothing counted.
    pub(crate) fn new(rules: Arc<Rules>) -> Self {
        Self {
            rules,
            pending: HashMap::new(),
            successes: HashMap::new(),
            responses: 0,
            refused: HashSet::new(),
            corrections: 0,
        }
    }

    /// The state of a run with these durable `events`.
    pub(crate) fn of(rules: Arc<Rules>, events: &[Envelope]) -> Self {
        let mut gates = Self::new(rules);
        for envelope in events {
            gates.see(&envelope.event);
        }
        gates
    }

    /// Count `event`, one the run stages.
    pub(crate) fn see(&mut self, event: &Event) {
        match event {
            Event::Assistant(response) => {
                self.responses += 1;
                self.pending.clear();
                for call in response.message.tool_uses() {
                    if !self.rules.referenced.contains(&call.name) {
                        continue;
                    }
                    let background = call.input.get("run_in_background");
                    let background = background.and_then(|flag| flag.as_bool()) == Some(true);
                    let known = (call.name.clone(), background);
                    self.pending.insert(call.id.clone(), known);
                }
            }
            Event::User(user) if user.subtype == UserEventSubtype::ToolResult => {
                let pending = std::mem::take(&mut self.pending);
                for block in &user.message.content {
                    let ContentBlock::ToolResult(result) = block else {
                        continue;
                    };
                    match pending.get(&result.tool_use_id) {
                        Some((tool, false)) if !result.is_error => {
                            *self.successes.entry(tool.clone()).or_default() += 1;
                        }
                        _ => {}
                    }
                }
            }
            Event::Policy(event) => match (event.rule, event.action) {
                (PolicyRule::Prerequisite, PolicyAction::Refuse) => {
                    let turn = self.responses;
                    (self.refused).extend(event.tool_use_id.iter().map(|id| (turn, id.clone())));
                }
                (PolicyRule::Completion, PolicyAction::Correct) => self.corrections += 1,
                _ => {}
            },
            _ => {}
        }
    }

    /// The tools of `tool`'s prerequisite that have not succeeded yet, as
    /// the refusal of a call of it; `None` when the call may run. The
    /// message names them only when `offered` says the model has each one:
    /// a tool a skill or the run's options hid from it is not named.
    pub(crate) fn refusal(&self, tool: &str, offered: &dyn Fn(&str) -> bool) -> Option<Refusal> {
        let required = self.rules.requires.get(tool)?;
        let missing: Vec<String> = (required.iter())
            .filter(|name| self.successes.get(*name).copied().unwrap_or(0) == 0)
            .cloned()
            .collect();
        if missing.is_empty() {
            return None;
        }
        let message = match missing.iter().all(|name| offered(name)) {
            true => {
                let list = quoted(&missing);
                format!(
                    "Tool '{tool}' cannot run yet: {list} must succeed first. Call {list}, wait \
                     for its result, then call '{tool}' again."
                )
            }
            false => format!("Tool '{tool}' cannot run yet: a prerequisite has not succeeded."),
        };
        Some(Refusal { missing, message })
    }

    /// The `policy` event of the refusal of the call `tool_use_id`, unless
    /// it was reported already.
    pub(crate) fn refused_event(
        &self,
        tool: &str,
        tool_use_id: &str,
        refusal: &Refusal,
    ) -> Option<PolicyEvent> {
        if (self.refused).contains(&(self.responses, tool_use_id.to_owned())) {
            return None;
        }
        Some(PolicyEvent {
            rule: PolicyRule::Prerequisite,
            action: PolicyAction::Refuse,
            tool: Some(tool.to_owned()),
            tool_use_id: Some(tool_use_id.to_owned()),
            gates: Vec::new(),
            missing: refusal.missing.clone(),
            attempt: 0,
            detail: refusal.message.clone(),
        })
    }

    /// How the final answer `text` fails the gates; `None` when it meets
    /// them all. The feedback names the tools a gate needs only when
    /// `offered` says the model has each one, as [`Gates::refusal`] does.
    ///
    /// A `text` that is a JSON object (a final tool's input, a structured
    /// answer, a hook's rewrite of either) is also judged as the strings it
    /// holds, as they read: the JSON text escapes what a pattern may name (a
    /// quote, a newline). A `forbids` check fails when it matches any of
    /// the texts, any other check when it passes on none.
    pub(crate) fn check(&self, text: &str, offered: &dyn Fn(&str) -> bool) -> Option<Failure> {
        let object = (self.rules.checks.iter().any(Option::is_some) && text.starts_with('{'))
            .then(|| serde_json::from_str::<JsonObject>(text).ok())
            .flatten();
        let decoded = object.as_ref().map(leaves).unwrap_or_default();
        let texts: Vec<&str> = [text]
            .into_iter()
            .chain(decoded.iter().map(String::as_str))
            .collect();
        let policy = &self.rules.policy;
        let (mut gates, mut missing, mut lines) = (Vec::new(), Vec::new(), Vec::new());
        for (gate, check) in policy.gates.iter().zip(&self.rules.checks) {
            let short: Vec<&String> = (gate.succeeded.iter())
                .filter(|tool| self.successes.get(*tool).copied().unwrap_or(0) < gate.min_calls)
                .collect();
            let failed = check.as_ref().is_some_and(|verifiers| {
                let fails = |text: &&str| verifiers.check(text).is_some();
                match gate.check {
                    Some(Check::Forbids { .. }) => texts.iter().any(fails),
                    _ => texts.iter().all(fails),
                }
            });
            if short.is_empty() && !failed {
                continue;
            }
            gates.push(gate.name.clone());
            for tool in &short {
                if !missing.contains(*tool) {
                    missing.push((*tool).clone());
                }
            }
            let line = match (&gate.feedback, failed) {
                (Some(feedback), _) => feedback.clone(),
                // Not the pattern or keywords: they are the operator's, and the
                // model that reads this may be a tenant's.
                (None, true) if short.is_empty() => format!(
                    "The requirement '{}' is not met: your final answer does not satisfy its \
                     check. Revise it and give your final answer again.",
                    gate.name
                ),
                (None, _) if !short.iter().all(|tool| offered(tool)) => format!(
                    "The requirement '{}' is not met: a required tool has not succeeded.",
                    gate.name
                ),
                (None, _) => {
                    let tools = quoted(&short.iter().map(|t| (*t).clone()).collect::<Vec<_>>());
                    let times = match gate.min_calls {
                        1 => String::new(),
                        n => format!(" at least {n} times each"),
                    };
                    format!(
                        "The requirement '{}' is not met: {tools} must have succeeded{times}. Call \
                         {}, then give your final answer again.",
                        gate.name,
                        if short.len() == 1 { "it" } else { "them" },
                    )
                }
            };
            lines.push(line);
        }
        (!gates.is_empty()).then(|| Failure {
            gates,
            missing,
            feedback: lines.join("\n"),
        })
    }

    /// Whether another correction may go to the model.
    pub(crate) fn can_correct(&self) -> bool {
        self.corrections < self.rules.policy.corrections()
    }

    /// The `policy` event of a correction for `failure`, counted as spent.
    pub(crate) fn correction(&self, failure: &Failure) -> PolicyEvent {
        PolicyEvent {
            rule: PolicyRule::Completion,
            action: PolicyAction::Correct,
            tool: None,
            tool_use_id: None,
            gates: failure.gates.clone(),
            missing: failure.missing.clone(),
            attempt: u64::from(self.corrections) + 1,
            detail: failure.feedback.clone(),
        }
    }

    /// The `policy` event of the stop that ends the run on `failure`, a
    /// final answer a `stop` hook rewrote into one the gates do not accept.
    /// No correction goes back: the hook would rewrite the next answer too.
    pub(crate) fn rewritten(&self, failure: &Failure) -> PolicyEvent {
        PolicyEvent {
            detail: format!(
                "a stop hook rewrote the final answer into one that does not meet the completion \
                 gates: {}",
                failure.feedback
            ),
            ..self.stop(failure)
        }
    }

    /// The `policy` event of the stop `failure` ends the run on.
    pub(crate) fn stop(&self, failure: &Failure) -> PolicyEvent {
        let max = self.rules.policy.corrections();
        let detail = match max {
            0 => "the final answer does not meet the completion gates".to_owned(),
            n => format!(
                "the final answer still does not meet the completion gates after {n} correction(s)"
            ),
        };
        PolicyEvent {
            rule: PolicyRule::Completion,
            action: PolicyAction::Stop,
            tool: None,
            tool_use_id: None,
            gates: failure.gates.clone(),
            missing: failure.missing.clone(),
            attempt: u64::from(self.corrections),
            detail: format!("{detail}: {}", failure.feedback),
        }
    }
}

/// The strings in `input`, keys and values, at any depth, as they read.
fn leaves(input: &JsonObject) -> Vec<String> {
    fn walk(value: &JsonValue, found: &mut Vec<String>) {
        match value {
            JsonValue::String(text) => found.push(text.clone()),
            JsonValue::Array(items) => items.iter().for_each(|item| walk(item, found)),
            JsonValue::Object(map) => {
                for (key, item) in map {
                    found.push(key.clone());
                    walk(item, found);
                }
            }
            _ => {}
        }
    }
    let mut found = Vec::new();
    for (key, item) in input {
        found.push(key.clone());
        walk(item, &mut found);
    }
    found
}

/// `names` quoted and joined for a sentence.
fn quoted(names: &[String]) -> String {
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    quoted.join(", ")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::events::WIRE_VERSION;
    use crate::events::kinds::{AssistantEvent, UserEvent};
    use crate::ids::RunId;
    use crate::json::JsonObject;
    use crate::message::{Message, Role, ToolResultBlock, ToolUseBlock};
    use crate::run::StopReason;

    fn rules(policy: serde_json::Value) -> Arc<Rules> {
        let policy: ReliabilityPolicy = serde_json::from_value(policy).unwrap();
        Arc::new(Rules::new(policy).unwrap())
    }

    fn policy() -> Arc<Rules> {
        rules(json!({
            "prerequisites": [{"tool": "Update", "requires": ["Lookup"]}],
            "gates": [{"name": "tests", "succeeded": ["Test"], "min_calls": 2}],
        }))
    }

    fn asked(calls: &[(&str, &str, serde_json::Value)]) -> Event {
        let blocks = calls.iter().map(|(id, name, input)| {
            ContentBlock::ToolUse(ToolUseBlock {
                id: (*id).into(),
                name: (*name).into(),
                input: input.as_object().cloned().unwrap_or_else(JsonObject::new),
                raw_input: None,
            })
        });
        Event::Assistant(AssistantEvent {
            message: Message::new(Role::Assistant, blocks.collect()),
            stop_reason: StopReason::ToolUse,
        })
    }

    fn answered(answers: &[(&str, bool)]) -> Event {
        let blocks = answers
            .iter()
            .map(|(id, failed)| ContentBlock::ToolResult(ToolResultBlock::text(*id, "r", *failed)));
        Event::User(UserEvent {
            message: Message::new(Role::User, blocks.collect()),
            subtype: UserEventSubtype::ToolResult,
            steer_id: None,
        })
    }

    #[test]
    fn a_call_unlocks_only_with_a_foreground_success_of_an_earlier_batch() {
        let mut gates = Gates::new(policy());
        assert!(
            gates.refusal("Lookup", &|_| true).is_none(),
            "nothing requires Lookup"
        );
        let refusal = gates.refusal("Update", &|_| true).unwrap();
        assert_eq!(refusal.missing, ["Lookup"]);
        assert!(refusal.message.contains("'Lookup' must succeed first"));

        gates.see(&asked(&[("a", "Lookup", json!({}))]));
        assert!(
            gates.refusal("Update", &|_| true).is_some(),
            "its result is not in"
        );
        gates.see(&answered(&[("a", true)]));
        assert!(
            gates.refusal("Update", &|_| true).is_some(),
            "an error does not count"
        );
        let background = json!({"run_in_background": true});
        gates.see(&asked(&[("b", "Lookup", background)]));
        gates.see(&answered(&[("b", false)]));
        assert!(
            gates.refusal("Update", &|_| true).is_some(),
            "an ack does not"
        );
        let foreground = json!({"run_in_background": false});
        gates.see(&asked(&[("c", "Lookup", foreground)]));
        gates.see(&answered(&[("c", false)]));
        assert!(gates.refusal("Update", &|_| true).is_none());
    }

    #[test]
    fn a_refusal_is_reported_once_and_a_correction_counts() {
        let mut gates = Gates::new(policy());
        let refusal = gates.refusal("Update", &|_| true).unwrap();
        let event = gates.refused_event("Update", "x", &refusal).unwrap();
        gates.see(&Event::Policy(event));
        assert!(gates.refused_event("Update", "x", &refusal).is_none());
        assert!(gates.refused_event("Update", "y", &refusal).is_some());

        let failure = gates.check("done", &|_| true).unwrap();
        assert_eq!(failure.gates, ["tests"]);
        assert!(failure.feedback.contains("at least 2 times each"));
        assert!(gates.can_correct());
        for spent in 1..=2 {
            let event = gates.correction(&failure);
            assert_eq!(event.attempt, spent);
            gates.see(&Event::Policy(event));
        }
        assert!(!gates.can_correct());
        assert_eq!(gates.stop(&failure).attempt, 2);
    }

    #[test]
    fn a_refusal_does_not_name_a_tool_the_model_was_not_offered() {
        let gates = Gates::new(policy());
        let hidden = |name: &str| name != "Lookup";
        let refusal = gates.refusal("Update", &hidden).unwrap();
        assert!(!refusal.message.contains("Lookup"), "{}", refusal.message);
        assert!(refusal.message.contains("'Update'"));
        assert_eq!(refusal.missing, ["Lookup"], "the audit event keeps it");
    }

    #[test]
    fn a_provider_that_repeats_an_id_is_refused_and_reported_again_next_turn() {
        let mut gates = Gates::new(policy());
        gates.see(&asked(&[("x", "Update", json!({}))]));
        let refusal = gates.refusal("Update", &|_| true).unwrap();
        let event = gates.refused_event("Update", "x", &refusal).unwrap();
        gates.see(&Event::Policy(event));
        assert!(gates.refused_event("Update", "x", &refusal).is_none());
        // The next response reuses the id: its refusal is a new one.
        gates.see(&answered(&[("x", true)]));
        gates.see(&asked(&[("x", "Update", json!({}))]));
        assert!(gates.refused_event("Update", "x", &refusal).is_some());
    }

    #[test]
    fn the_state_read_back_from_the_log_is_the_live_one() {
        let rules = policy();
        let events = [
            asked(&[("a", "Lookup", json!({})), ("b", "Test", json!({}))]),
            answered(&[("a", false), ("b", false)]),
            asked(&[("c", "Test", json!({}))]),
            answered(&[("c", false)]),
            Event::Policy(PolicyEvent {
                rule: PolicyRule::Completion,
                action: PolicyAction::Correct,
                tool: None,
                tool_use_id: None,
                gates: vec!["tests".into()],
                missing: vec![],
                attempt: 1,
                detail: String::new(),
            }),
            asked(&[("d", "Test", json!({}))]),
        ];
        let mut live = Gates::new(rules.clone());
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
        let read = Gates::of(rules, &envelopes);
        assert_eq!(
            (read.pending, read.successes.clone(), read.corrections),
            (
                live.pending.clone(),
                live.successes.clone(),
                live.corrections
            )
        );
        assert!(
            live.check("done", &|_| true).is_none(),
            "two Test successes"
        );
        assert_eq!(live.corrections, 1);
    }

    #[test]
    fn a_check_gate_judges_the_answer_and_a_feedback_replaces_the_default() {
        let gates = Gates::new(rules(json!({"gates": [
            {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}},
            {"name": "polite", "check": {"type": "keywords", "keywords": ["please"]},
             "feedback": "Say please."},
        ]})));
        let failure = gates.check("plain", &|_| true).unwrap();
        assert_eq!(failure.gates, ["cites", "polite"]);
        assert!(failure.missing.is_empty());
        let lines: Vec<_> = failure.feedback.lines().collect();
        assert!(lines[0].contains("'cites' is not met"), "{lines:?}");
        assert!(
            !failure.feedback.contains("\\d"),
            "the pattern stays hidden"
        );
        assert_eq!(lines[1], "Say please.");
        assert!(gates.check("please see [1]", &|_| true).is_none());
    }

    #[test]
    fn a_correction_does_not_name_a_tool_the_model_was_not_offered() {
        let gates = Gates::new(policy());
        let hidden = |name: &str| name != "Test";
        let failure = gates.check("done", &hidden).unwrap();
        assert!(!failure.feedback.contains("Test"), "{}", failure.feedback);
        assert!(failure.feedback.contains("'tests'"));
        assert_eq!(failure.missing, ["Test"], "the audit event keeps it");
        let named = gates.check("done", &|_| true).unwrap();
        assert!(named.feedback.contains("'Test'"), "{}", named.feedback);
    }

    #[test]
    fn a_policy_that_cannot_run_says_why() {
        let offers = |name: &str| ["A", "B", "C"].contains(&name);
        let found = |policy: serde_json::Value| {
            let policy: ReliabilityPolicy = serde_json::from_value(policy).unwrap();
            policy.problem(offers, Some("Final"))
        };
        let problem = |policy: serde_json::Value| {
            found(policy).map(|(PolicyProblem::Own(text) | PolicyProblem::Joint(text))| text)
        };
        let need = |tool: &str, requires: &[&str]| json!({"tool": tool, "requires": requires});
        assert!(problem(json!({})).is_none());
        let ring =
            json!({"prerequisites": [need("A", &["B"]), need("B", &["C"]), need("C", &["A"])]});
        assert!(problem(ring).unwrap().contains("lead back to"));
        let chain = json!({"prerequisites": [need("A", &["B"]), need("B", &["C"])]});
        assert!(problem(chain).is_none(), "a chain is not a cycle");
        let diamond = json!({"prerequisites": [need("A", &["B", "C"]), need("B", &["C"])]});
        assert!(
            problem(diamond).is_none(),
            "a shared requirement is not one"
        );
        let named = json!({"gates": [{"name": "g", "succeeded": ["Final"]}]});
        assert!(problem(named).unwrap().contains("final tool"));
    }

    #[test]
    fn a_problem_says_whether_the_policy_alone_can_have_it() {
        let offers = |name: &str| ["A", "B"].contains(&name);
        let kind = |policy: serde_json::Value| {
            let policy: ReliabilityPolicy = serde_json::from_value(policy).unwrap();
            policy
                .problem(offers, None)
                .map(|problem| matches!(problem, PolicyProblem::Own(_)))
        };
        let need = |tool: &str, requires: &[&str]| json!({"tool": tool, "requires": requires});
        // The text of one rule: whoever wrote it got it wrong.
        assert_eq!(kind(json!({"gates": [{"name": "mine"}]})), Some(true));
        assert_eq!(
            kind(json!({"gates": [{"name": " ", "succeeded": ["A"]}]})),
            Some(true)
        );
        assert_eq!(
            kind(json!({"prerequisites": [need("A", &["A"])]})),
            Some(true)
        );
        // What depends on the agent's tools or on rules read together.
        let unknown = json!({"gates": [{"name": "g", "succeeded": ["Nope"]}]});
        assert_eq!(kind(unknown), Some(false));
        let ring = json!({"prerequisites": [need("A", &["B"]), need("B", &["A"])]});
        assert_eq!(kind(ring), Some(false));
    }

    #[test]
    fn a_child_keeps_the_prerequisites_and_drops_the_gates() {
        let parent: ReliabilityPolicy = serde_json::from_value(json!({
            "prerequisites": [{"tool": "Update", "requires": ["Lookup"]}],
            "gates": [{"name": "tests", "succeeded": ["Test"]}],
            "max_corrections": 5,
        }))
        .unwrap();
        let child = parent.for_child();
        assert_eq!(child.prerequisites, parent.prerequisites);
        assert!(child.gates.is_empty());
    }
}
