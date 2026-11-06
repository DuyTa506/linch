//! [`AgentSpec`]: the serde (tier 0) part of an agent, and the run contract
//! digest built from it.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::canonical::{CanonicalError, Digest, digest_with_domain};
use crate::compaction::CompactionSpec;
use crate::json::JsonObject;
use crate::message::SystemBlock;
use crate::provider::{ProviderRequest, ReasoningEffort, Thinking, ToolDefinition};
use crate::tool::ToolScope;

/// Domain separator for the run contract digest.
const CONTRACT_DOMAIN: &str = "linch.agent.contract";

fn default_concurrency() -> u32 {
    4
}

/// What an agent is, as data: the model, its instructions, and the request
/// and loop limits. Tools, hooks, permissions and stores are bound in code
/// by [`super::AgentBuilder`]; the spec names none of them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSpec {
    /// Model identifier sent with every request.
    pub model: String,
    /// System prompt; empty sends no system block.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub system_prompt: String,
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
}

impl AgentSpec {
    /// A spec for `model` with every other field at its default.
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            system_prompt: String::new(),
            max_turns: None,
            thinking: None,
            effort: None,
            max_output_tokens: None,
            max_tool_concurrency: default_concurrency(),
            call_timeout_ms: None,
            compaction: None,
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

/// The system prompt as a system block; none when it is empty.
pub(crate) fn prompt_block(spec: &AgentSpec) -> Vec<SystemBlock> {
    if spec.system_prompt.is_empty() {
        return Vec::new();
    }
    vec![SystemBlock {
        text: spec.system_prompt.clone(),
        cacheable: false,
    }]
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
            (50000, 4)
        );
    }
}
