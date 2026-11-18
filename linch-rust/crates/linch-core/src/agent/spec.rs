//! [`AgentSpec`]: the serde (tier 0) part of an agent, and the run contract
//! digest built from it.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::budget::RunBudget;
use crate::canonical::{CanonicalError, Digest, digest_with_domain};
use crate::compaction::CompactionSpec;
use crate::context_budget::ContextBudget;
use crate::gates::ReliabilityPolicy;
use crate::json::JsonObject;
use crate::loop_guard::LoopGuard;
use crate::message::SystemBlock;
use crate::provider::{
    OutputSchema, ProviderRequest, ReasoningEffort, Thinking, ToolChoice, ToolDefinition,
};
use crate::redaction::RedactionSpec;
use crate::reliability::ProviderRetry;
use crate::system::SystemConfig;
use crate::tool::ToolScope;
use crate::tool_cache::ToolCache;
use crate::tool_retry::ToolRetry;
use crate::truncation::TruncationRecovery;
use crate::verification::VerificationSpec;

/// Domain separator for the run contract digest.
const CONTRACT_DOMAIN: &str = "linch.agent.contract";

fn default_concurrency() -> u32 {
    4
}

/// What an agent is, as data: the model, its instructions, and the request
/// and loop limits. Tools, hooks, permissions and stores are bound in code
/// by [`super::AgentBuilder`]; the spec names none of them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSpec {
    /// Model identifier sent with every request.
    pub model: String,
    /// System prompt; empty sends no system block.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub system_prompt: String,
    /// The host's own system blocks, sent in order before the system
    /// prompt, each with its `cacheable` flag (Python's
    /// `SystemPromptConfig.blocks`). None may be blank.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub system_blocks: Vec<SystemBlock>,
    /// Named system prompt sections, text appended after the prompt, and
    /// whether the presets' own prompt text is left out (Python's
    /// `SystemPromptConfig`; see [`crate::system`]). The default changes
    /// nothing and is left off the wire, so a spec that does not set it
    /// keeps the contract it had before sections existed. A subagent's
    /// runs take it too: its system is the parent's, then its definition.
    #[serde(default, skip_serializing_if = "SystemConfig::is_default")]
    pub system: SystemConfig,
    /// A cap on the request-only context the `context_build` hooks add
    /// (retrieved memory, file memory), and what is dropped first when it
    /// does not fit; see [`crate::context_budget`]. `None` (the default)
    /// keeps every block and is left off the wire. A subagent's runs take
    /// it too.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_budget: Option<ContextBudget>,
    /// Most provider calls one run may make; `None` sets no limit. A run that
    /// would need one more call ends with status `failed` (Python's
    /// `max_turns` tail).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_turns: Option<u32>,
    /// Requested thinking behaviour; `None` keeps the provider default.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thinking: Option<Thinking>,
    /// Requested reasoning effort.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<ReasoningEffort>,
    /// Most tokens one response may generate.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u32>,
    /// Most read-scope tools that run at once (default 4; 0 counts as 1).
    #[serde(default = "default_concurrency")]
    pub max_tool_concurrency: u32,
    /// Longest one tool call may run, in milliseconds; `None` waits.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_timeout_ms: Option<u64>,
    /// When to summarize the older part of the provider view; `None`
    /// (the default) never compacts.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compaction: Option<CompactionSpec>,
    /// The token and USD cap of each root run and its subagent children
    /// (Python's `AgentConfig.budget`); `None` (the default) counts nothing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub budget: Option<RunBudget>,
    /// How a provider call that failed with a retryable error is retried
    /// on the same model: on by default, with Python's `RetryOptions`;
    /// `max_attempts: 1` turns it off. The default is left off the wire,
    /// so a spec that does not set it keeps the contract of a spec written
    /// before retries existed.
    #[serde(default, skip_serializing_if = "ProviderRetry::is_default")]
    pub provider_retry: ProviderRetry,
    /// Models to fail over to, in order, once a model has no retry left
    /// for a retryable failure; the run keeps the one it fails over to.
    /// Empty (the default) fails the run instead.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub fallback_models: Vec<String>,
    /// The JSON Schema the final answer of each run must satisfy (Python's
    /// `Agent.output_schema`); `None` (the default) takes any text. It is
    /// sent with every primary request, so the provider can constrain the
    /// answer, and the answer is checked against it before the run
    /// completes; the parsed object is the result's `structured_output`.
    /// A subagent's runs do not take it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<OutputSchema>,
    /// How many times an answer that breaks `output_schema` goes back to
    /// the model with the error before the run fails (Python's
    /// `structured_output_retries`, default 0: the first such answer fails
    /// the run). Each retry is a turn, so `max_turns` bounds them too. It
    /// also bounds a run's own schema (`RunOptions::output_schema`), so it
    /// may be set without `output_schema`.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub structured_output_retries: u32,
    /// Regex rules that scrub assistant responses, tool results and
    /// optionally the prompt before they are logged, streamed or shown to
    /// a hook (Python's `RedactionHook`; see [`crate::redaction`]).
    /// `None` (the default) scrubs nothing. A subagent's runs take it too.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub redaction: Option<RedactionSpec>,
    /// Checks the final answer must pass, and the retry budget they share
    /// with the `stop` hooks' retries (Python's `FinalAnswerVerifierHook`;
    /// see [`crate::verification`]). `None` (the default) checks nothing
    /// and leaves a `stop` hook one retry per run call. A subagent's runs
    /// do not take it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verification: Option<VerificationSpec>,
    /// How a text answer the output limit cut off is continued instead of
    /// taken as final (Python's `TruncationRecovery`; see
    /// [`crate::truncation`]). `None` (the default) takes it as it is.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub truncation_recovery: Option<TruncationRecovery>,
    /// When a stuck run ends `failed` with a `loop_guard` event (Python's
    /// `LoopGuard`; see [`crate::loop_guard`]): on by default with
    /// Python's limits of 3 identical tool calls and 3 failed batches in a
    /// row, and at most 25 turns in a row without a provider call. 0 turns
    /// a tool-call check off; the idle-turn bound is always on. The default
    /// is left off the wire, so a spec that does not set it keeps the
    /// contract of a spec written before the guard existed. A subagent's
    /// runs take it too.
    #[serde(default, skip_serializing_if = "LoopGuard::is_default")]
    pub loop_guard: LoopGuard,
    /// Whether and which tool the model must call (Python's
    /// `Agent.tool_choice`): `auto`, `none`, `required` or a named tool,
    /// sent with every primary request of a run that does not bring its
    /// own (`RunOptions::tool_choice`), but on a turn that offers no tool.
    /// A forced choice holds every turn, so such a run ends through the
    /// final tool, or a limit. `None` (the default) sends none; the
    /// provider's default is `auto`. The agent refuses a named tool it does
    /// not have, and a choice its provider refuses
    /// ([`crate::Provider::tool_choice_refusal`]). A subagent's runs do not
    /// take it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_choice: Option<ToolChoice>,
    /// A registered tool whose call ends the run instead of running
    /// (Python's `Agent.final_tool_name`): its input, checked against the
    /// output schema when there is one, is the result's
    /// `structured_output`. `None` (the default) has none, unless the run
    /// has an output schema its provider cannot take natively
    /// ([`crate::Provider::native_output_schema`]): the run then offers a
    /// final tool of its own, named after the schema. A subagent's runs do
    /// not take it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_tool_name: Option<String>,
    /// Runs a failed tool call again (Python's `Agent.tool_retry`; see
    /// [`crate::tool_retry`]): only a tool that declares itself idempotent,
    /// only after an error of a class the policy lists, with a durable
    /// `tool_retry` event before each attempt. `None` (the default) never
    /// retries and is left off the wire, so a spec that does not set it
    /// keeps the contract of a spec written before this existed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_retry: Option<ToolRetry>,
    /// Reuses the result of a named read tool for an identical call, after
    /// permission and the hooks (see [`crate::tool_cache`]); entries expire,
    /// belong to the agent's namespace and are cleared by a write tool.
    /// `None` (the default) caches nothing and is left off the wire, so a
    /// spec that does not set it keeps the contract of a spec written
    /// before this existed. A subagent's runs do not use it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_cache: Option<ToolCache>,
    /// Which tools must succeed before another may run, and what a run must
    /// have done before it may end (see [`crate::gates`]). `None` (the
    /// default) checks nothing and is left off the wire, so a spec that
    /// does not set it keeps the contract of a spec written before
    /// policies existed. A subagent's runs take its prerequisites, not its
    /// gates.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub policy: Option<ReliabilityPolicy>,
}

fn is_zero(value: &u32) -> bool {
    *value == 0
}

impl AgentSpec {
    /// A spec for `model` with every other field at its default.
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            system_prompt: String::new(),
            system_blocks: Vec::new(),
            system: SystemConfig::default(),
            context_budget: None,
            max_turns: None,
            thinking: None,
            effort: None,
            max_output_tokens: None,
            max_tool_concurrency: default_concurrency(),
            call_timeout_ms: None,
            compaction: None,
            budget: None,
            provider_retry: ProviderRetry::default(),
            fallback_models: Vec::new(),
            output_schema: None,
            structured_output_retries: 0,
            redaction: None,
            verification: None,
            truncation_recovery: None,
            loop_guard: LoopGuard::default(),
            tool_choice: None,
            final_tool_name: None,
            tool_retry: None,
            tool_cache: None,
            policy: None,
        }
    }

    /// The per-call tool timeout.
    pub fn call_timeout(&self) -> Option<Duration> {
        self.call_timeout_ms.map(Duration::from_millis)
    }

    /// A request for this spec with `tools` and `messages`, and no system
    /// blocks: the agent adds its own.
    pub(crate) fn request(
        &self,
        tools: Vec<ToolDefinition>,
        messages: Vec<crate::message::Message>,
    ) -> ProviderRequest {
        ProviderRequest {
            model: self.model.clone(),
            system: Vec::new(),
            tools,
            appended_tools: Vec::new(),
            context_tail: 0,
            messages,
            max_output_tokens: self.max_output_tokens,
            temperature: None,
            stop_sequences: Vec::new(),
            effort: self.effort,
            thinking: self.thinking.clone(),
            cache_control: None,
            output_schema: None,
            tool_choice: None,
        }
    }
}

impl AgentSpec {
    /// Whether the presets leave their own prompt text out of
    /// `system_prompt` (`system.replace_defaults`).
    pub fn replaces_defaults(&self) -> bool {
        self.system.replace_defaults
    }
}

/// Digest of the run contract: the spec, each tool's definition and scope,
/// the hook policy digest, and the `features` that shape
/// requests beyond the spec (the skills, a subagent's definition). A run
/// stores it at creation; resume and approve refuse an agent whose
/// contract differs. With no features the digest is the one an agent
/// without them always had.
pub(crate) fn contract_digest(
    spec: &AgentSpec,
    tools: &[(ToolDefinition, ToolScope)],
    hooks: Digest,
    features: &JsonObject,
) -> Result<Digest, CanonicalError> {
    let tools: Vec<_> = (tools.iter())
        .map(|(definition, scope)| json!({"definition": definition, "scope": scope}))
        .collect();
    let mut value = json!({
        "spec": spec,
        "tools": tools,
        "hooks": hooks.to_string(),
    });
    if let Some(object) = value.as_object_mut() {
        object.extend(features.clone());
    }
    digest_with_domain(CONTRACT_DOMAIN, &value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_spec_reads_strictly_with_defaults() {
        let spec: AgentSpec = serde_json::from_value(json!({"model": "m"})).unwrap();
        assert_eq!(spec, AgentSpec::new("m"));
        assert_eq!(spec.max_tool_concurrency, 4);
        let extra = json!({"model": "m", "tools": ["Read"]});
        assert!(serde_json::from_value::<AgentSpec>(extra).is_err());
    }

    #[test]
    fn the_contract_covers_spec_tools_and_hooks() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let spec = AgentSpec::new("m");
        let base = contract_digest(&spec, &[], hooks, &none).unwrap();
        assert_eq!(
            base,
            contract_digest(&spec.clone(), &[], hooks, &none).unwrap()
        );
        let mut other = spec.clone();
        other.system_prompt = "be brief".into();
        assert_ne!(base, contract_digest(&other, &[], hooks, &none).unwrap());
        let tool = ToolDefinition {
            name: "Read".into(),
            description: String::new(),
            input_schema: Default::default(),
        };
        let read =
            contract_digest(&spec, &[(tool.clone(), ToolScope::Read)], hooks, &none).unwrap();
        assert_ne!(base, read);
        let exec = contract_digest(&spec, &[(tool, ToolScope::Exec)], hooks, &none).unwrap();
        assert_ne!(read, exec, "the scope counts");
        let digest = crate::canonical::digest(&json!(1)).unwrap();
        assert_ne!(base, contract_digest(&spec, &[], digest, &none).unwrap());
        let mut compacting = spec.clone();
        compacting.compaction = Some(CompactionSpec::new(1000));
        assert_ne!(
            base,
            contract_digest(&compacting, &[], hooks, &none).unwrap()
        );
        let mut features = JsonObject::new();
        features.insert("skills".into(), json!([]));
        assert_ne!(base, contract_digest(&spec, &[], hooks, &features).unwrap());
    }

    /// The contract of a spec with no output schema is the one it had
    /// before structured output existed, so its runs still resume.
    #[test]
    fn a_spec_without_an_output_schema_keeps_its_contract() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let spec = AgentSpec::new("m");
        let digest = contract_digest(&spec, &[], hooks, &none).unwrap();
        assert_eq!(
            digest.to_string(),
            "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300"
        );
        let mut schema = json!({"name": "answer", "schema": {"type": "object"}});
        let mut shaped = spec.clone();
        shaped.output_schema = Some(serde_json::from_value(schema.clone()).unwrap());
        let with = contract_digest(&shaped, &[], hooks, &none).unwrap();
        assert_ne!(digest, with, "the schema is part of the contract");
        schema["schema"]["required"] = json!(["x"]);
        shaped.output_schema = Some(serde_json::from_value(schema).unwrap());
        assert_ne!(with, contract_digest(&shaped, &[], hooks, &none).unwrap());
        shaped.structured_output_retries = 2;
        let retrying = contract_digest(&shaped, &[], hooks, &none).unwrap();
        assert_ne!(with, retrying, "the retry bound is part of the contract");
        let wire = serde_json::to_value(&shaped).unwrap();
        assert_eq!(wire["structured_output_retries"], 2);
        assert_eq!(serde_json::from_value::<AgentSpec>(wire).unwrap(), shaped);
    }

    /// Sections and a context budget are off the wire when unset (or the
    /// default), so the contract of a spec without them is the one it had
    /// before they existed; set, each is part of the contract.
    #[test]
    fn system_sections_and_the_context_budget_are_in_the_contract_only_when_set() {
        use crate::system::SystemSection;
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let unset = AgentSpec::new("m");
        let digest = contract_digest(&unset, &[], hooks, &none).unwrap();
        assert_eq!(
            digest.to_string(),
            "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300",
            "the contract of a spec without sections or a budget did not move"
        );
        let wire = serde_json::to_value(&unset).unwrap();
        assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        let empty: AgentSpec = serde_json::from_value(json!({"model": "m", "system": {}})).unwrap();
        assert_eq!(empty, unset, "an empty system is the unset one");

        let mut sectioned = unset.clone();
        sectioned.system.sections = vec![SystemSection::new("policy", "Be brief.").order(1)];
        let with = contract_digest(&sectioned, &[], hooks, &none).unwrap();
        assert_eq!(
            with.to_string(),
            "e4fdbf9eea6cee642f153cf512a907cd59a9227689c77adbd3d80d2e69459957",
            "the contract of a spec with one section"
        );
        let mut other = sectioned.clone();
        other.system.sections[0].text = "Be long.".into();
        assert_ne!(with, contract_digest(&other, &[], hooks, &none).unwrap());
        other = sectioned.clone();
        other.system.sections[0].cacheable = false;
        assert_ne!(with, contract_digest(&other, &[], hooks, &none).unwrap());
        other = sectioned.clone();
        other.system.append = Some("tail".into());
        assert_ne!(with, contract_digest(&other, &[], hooks, &none).unwrap());
        other = sectioned.clone();
        other.system.replace_defaults = true;
        assert_ne!(with, contract_digest(&other, &[], hooks, &none).unwrap());

        let mut budgeted = unset.clone();
        budgeted.context_budget = Some(ContextBudget::new(500));
        let capped = contract_digest(&budgeted, &[], hooks, &none).unwrap();
        assert_eq!(
            capped.to_string(),
            "3983dcff71e93a5e2c2550270716f697408d0b7ec6f5d2f0eb5c47bd864564b8",
            "the contract of a spec with a 500 token budget"
        );
        assert_ne!(digest, capped);
        let mut lower = budgeted.clone();
        lower.context_budget = Some(ContextBudget::new(499));
        assert_ne!(capped, contract_digest(&lower, &[], hooks, &none).unwrap());
        let mut ordered = budgeted.clone();
        ordered.context_budget.as_mut().unwrap().drop_order = vec!["memory".into()];
        assert_ne!(
            capped,
            contract_digest(&ordered, &[], hooks, &none).unwrap()
        );
        let wire = serde_json::to_value(&budgeted).unwrap();
        assert_eq!(wire["context_budget"], json!({"max_tokens": 500}));
    }

    #[test]
    fn a_spec_reads_provider_retry_and_fallback_models() {
        let read: AgentSpec = serde_json::from_value(json!({
            "model": "m",
            "provider_retry": {"max_attempts": 3},
            "fallback_models": ["n", "o"],
        }))
        .unwrap();
        let retry = &read.provider_retry;
        assert_eq!(
            (retry.max_attempts, retry.base_delay_ms, retry.max_delay_ms),
            (3, 1000, 30000)
        );
        assert_eq!(read.fallback_models, ["n", "o"]);
        let typo = json!({"model": "m", "provider_retry": {"attempts": 3}});
        assert!(serde_json::from_value::<AgentSpec>(typo).is_err());
        let null = json!({"model": "m", "provider_retry": null});
        assert!(
            serde_json::from_value::<AgentSpec>(null).is_err(),
            "off is max_attempts 1"
        );
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let base = contract_digest(&AgentSpec::new("m"), &[], hooks, &none).unwrap();
        assert_ne!(base, contract_digest(&read, &[], hooks, &none).unwrap());
    }

    /// Retries are on unless the spec turns them off: unset, `{}` and the
    /// defaults spelled out are one spec, with the wire shape (and so the
    /// contract) of a spec written before retries existed; one attempt is
    /// off, and a contract of its own.
    #[test]
    fn retries_are_on_by_default_and_off_with_one_attempt() {
        let unset = AgentSpec::new("m");
        assert_eq!(unset.provider_retry, ProviderRetry::default());
        assert_eq!(unset.provider_retry.max_attempts, 5);
        for on in [
            json!({}),
            serde_json::to_value(ProviderRetry::default()).unwrap(),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "provider_retry": on})).unwrap();
            assert_eq!(read, unset);
            let wire = serde_json::to_value(&read).unwrap();
            assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        }
        let off: AgentSpec = serde_json::from_value(json!({
            "model": "m",
            "provider_retry": {"max_attempts": 1},
        }))
        .unwrap();
        assert_eq!(off.provider_retry.max_attempts, 1);
        let wire = serde_json::to_value(&off).unwrap();
        assert_eq!(
            wire["provider_retry"],
            json!({"max_attempts": 1, "base_delay_ms": 1000, "max_delay_ms": 30000, "jitter": 0.2})
        );
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let on = contract_digest(&unset, &[], hooks, &none).unwrap();
        assert_ne!(on, contract_digest(&off, &[], hooks, &none).unwrap());
    }

    /// The guard is on unless the spec changes it: unset, `{}` and the
    /// defaults spelled out are one spec, with the wire shape and the
    /// contract digest of a spec written before the guard existed, so
    /// in-flight runs still resume. Any other guard is on the wire and a
    /// contract of its own.
    #[test]
    fn the_loop_guard_is_on_by_default_and_off_the_wire() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let unset = AgentSpec::new("m");
        assert_eq!(unset.loop_guard, LoopGuard::default());
        let golden = "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300";
        for on in [
            json!({}),
            serde_json::to_value(LoopGuard::default()).unwrap(),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "loop_guard": on})).unwrap();
            assert_eq!(read, unset);
            let wire = serde_json::to_value(&read).unwrap();
            assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
            let digest = contract_digest(&read, &[], hooks, &none).unwrap();
            assert_eq!(digest.to_string(), golden);
        }
        let base = contract_digest(&unset, &[], hooks, &none).unwrap();
        for changed in [
            json!({"max_identical_tool_calls": 0}),
            json!({"max_consecutive_failures": 5}),
            json!({"max_idle_turns": 10}),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "loop_guard": changed})).unwrap();
            let wire = serde_json::to_value(&read).unwrap();
            assert!(wire.get("loop_guard").is_some(), "{wire}");
            assert_eq!(serde_json::from_value::<AgentSpec>(wire).unwrap(), read);
            let digest = contract_digest(&read, &[], hooks, &none).unwrap();
            assert_ne!(base, digest, "{changed}");
        }
        for refused in [json!(null), json!({"force_final_answer": true})] {
            let spec = json!({"model": "m", "loop_guard": refused});
            assert!(serde_json::from_value::<AgentSpec>(spec).is_err());
        }
    }

    /// Retry is off unless the spec sets it: an unset spec is on neither the
    /// wire nor in the contract, with the digest of a spec written before
    /// `tool_retry` existed; any policy, `{}` included, is a contract of its
    /// own.
    #[test]
    fn tool_retry_is_off_the_wire_and_the_contract_unless_set() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let unset = AgentSpec::new("m");
        assert_eq!(unset.tool_retry, None);
        let wire = serde_json::to_value(&unset).unwrap();
        assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        let golden = "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300";
        let base = contract_digest(&unset, &[], hooks, &none).unwrap();
        assert_eq!(base.to_string(), golden);
        for policy in [
            json!({}),
            json!({"max_attempts": 4}),
            json!({"retry_on": ["timeout"]}),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "tool_retry": policy})).unwrap();
            let wire = serde_json::to_value(&read).unwrap();
            assert!(wire.get("tool_retry").is_some(), "{wire}");
            assert_eq!(serde_json::from_value::<AgentSpec>(wire).unwrap(), read);
            let digest = contract_digest(&read, &[], hooks, &none).unwrap();
            assert_ne!(base, digest, "{policy}");
        }
        for refused in [
            json!(null),
            json!({"jitter": 0.1}),
            json!({"max_attempts": 0}),
        ] {
            let spec = json!({"model": "m", "tool_retry": refused});
            let read = serde_json::from_value::<AgentSpec>(spec);
            let ok = read.is_ok_and(|spec| spec.tool_retry.is_some_and(|r| r.problem().is_none()));
            assert!(!ok, "{refused}");
        }
    }

    /// A spec without `tool_cache` has the wire form and contract it had
    /// before the field existed; a cache, with whatever fields, is a
    /// contract of its own, and a policy that names no tool is refused.
    #[test]
    fn tool_cache_is_off_the_wire_and_the_contract_unless_set() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let unset = AgentSpec::new("m");
        assert_eq!(unset.tool_cache, None);
        let wire = serde_json::to_value(&unset).unwrap();
        assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        let golden = "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300";
        let base = contract_digest(&unset, &[], hooks, &none).unwrap();
        assert_eq!(base.to_string(), golden);
        for cache in [
            json!({"tools": ["Read"]}),
            json!({"tools": ["Read"], "ttl_ms": 10}),
            json!({"tools": ["Read"], "invalidate_on_write": false}),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "tool_cache": cache})).unwrap();
            let wire = serde_json::to_value(&read).unwrap();
            assert!(wire.get("tool_cache").is_some(), "{wire}");
            assert_eq!(serde_json::from_value::<AgentSpec>(wire).unwrap(), read);
            let digest = contract_digest(&read, &[], hooks, &none).unwrap();
            assert_ne!(base, digest, "{cache}");
        }
        for refused in [json!(null), json!({}), json!({"tools": []})] {
            let spec = json!({"model": "m", "tool_cache": refused});
            let read = serde_json::from_value::<AgentSpec>(spec);
            let ok = read.is_ok_and(|spec| spec.tool_cache.is_some_and(|c| c.problem().is_none()));
            assert!(!ok, "{refused}");
        }
    }

    /// A spec with no policy is off, off the wire and the contract it had
    /// before policies existed; any policy, an empty one included, is on
    /// the wire and a contract of its own.
    #[test]
    fn a_spec_without_a_policy_keeps_its_wire_shape_and_contract() {
        let hooks = crate::hooks::Hooks::default().policy_digest();
        let none = JsonObject::new();
        let unset = AgentSpec::new("m");
        assert_eq!(unset.policy, None);
        let wire = serde_json::to_value(&unset).unwrap();
        assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        let golden = "a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300";
        let digest = contract_digest(&unset, &[], hooks, &none).unwrap();
        assert_eq!(digest.to_string(), golden);
        let null: AgentSpec =
            serde_json::from_value(json!({"model": "m", "policy": null})).unwrap();
        assert_eq!(null, unset, "null is unset");

        let mut seen = vec![golden.to_owned()];
        for policy in [
            json!({}),
            json!({"max_corrections": 5}),
            json!({"max_corrections": 2}),
            json!({"prerequisites": [{"tool": "b", "requires": ["a"]}]}),
            json!({"gates": [{"name": "g", "succeeded": ["a"]}]}),
            json!({"gates": [{"name": "g", "succeeded": ["a"], "min_calls": 2}]}),
        ] {
            let read: AgentSpec =
                serde_json::from_value(json!({"model": "m", "policy": policy})).unwrap();
            let wire = serde_json::to_value(&read).unwrap();
            assert_eq!(wire["policy"], policy, "the policy is on the wire as set");
            assert_eq!(serde_json::from_value::<AgentSpec>(wire).unwrap(), read);
            let digest = contract_digest(&read, &[], hooks, &none)
                .unwrap()
                .to_string();
            assert!(!seen.contains(&digest), "{policy}");
            seen.push(digest);
        }
        let typo = json!({"model": "m", "policy": {"gate": []}});
        assert!(serde_json::from_value::<AgentSpec>(typo).is_err());
    }

    #[test]
    fn a_spec_without_compaction_keeps_its_wire_shape() {
        // So the contract of every spec written before compaction existed
        // is unchanged.
        let wire = serde_json::to_value(AgentSpec::new("m")).unwrap();
        assert_eq!(wire, json!({"model": "m", "max_tool_concurrency": 4}));
        let read: AgentSpec = serde_json::from_value(json!({
            "model": "m",
            "compaction": {"threshold_tokens": 50000, "keep_recent_turns": 4},
        }))
        .unwrap();
        let compaction = read.compaction.unwrap();
        assert_eq!(
            (compaction.threshold_tokens, compaction.keep_recent_turns),
            (Some(50000), 4)
        );
    }
}
