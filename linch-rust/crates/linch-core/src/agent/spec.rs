//! [`AgentSpec`]: the serde (tier 0) part of an agent, and the run contract
//! digest built from it.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::budget::RunBudget;
use crate::canonical::{CanonicalError, Digest, digest_with_domain};
use crate::compaction::CompactionSpec;
use crate::json::JsonObject;
use crate::message::SystemBlock;
use crate::provider::{OutputSchema, ProviderRequest, ReasoningEffort, Thinking, ToolDefinition};
use crate::redaction::RedactionSpec;
use crate::reliability::ProviderRetry;
use crate::tool::ToolScope;

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

/// The spec's system blocks: the host's in order, then the system prompt
/// unless it is empty.
pub(crate) fn system_blocks(spec: &AgentSpec) -> Vec<SystemBlock> {
    let mut system = spec.system_blocks.clone();
    if !spec.system_prompt.is_empty() {
        system.push(SystemBlock {
            text: spec.system_prompt.clone(),
            cacheable: false,
        });
    }
    system
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
