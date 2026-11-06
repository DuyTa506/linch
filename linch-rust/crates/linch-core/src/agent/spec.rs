//! [`AgentSpec`]: the serde (tier 0) part of an agent, and the run contract
//! digest built from it.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::canonical::{CanonicalError, Digest, digest_with_domain};
use crate::message::SystemBlock;
use crate::provider::{ProviderRequest, ReasoningEffort, Thinking, ToolDefinition};

/// Domain separator for the run contract digest.
const CONTRACT_DOMAIN: &str = "linch.agent.contract";

fn default_concurrency() -> usize {
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
    pub max_tool_concurrency: usize,
    /// Longest one tool call may run, in milliseconds; `None` waits.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_timeout_ms: Option<u64>,
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
        }
    }

    /// The per-call tool timeout.
    pub fn call_timeout(&self) -> Option<Duration> {
        self.call_timeout_ms.map(Duration::from_millis)
    }

    /// A request for this spec with `tools` and `messages`.
    pub(crate) fn request(
        &self,
        tools: Vec<ToolDefinition>,
        messages: Vec<crate::message::Message>,
    ) -> ProviderRequest {
        let system = if self.system_prompt.is_empty() {
            Vec::new()
        } else {
            vec![SystemBlock {
                text: self.system_prompt.clone(),
                cacheable: false,
            }]
        };
        ProviderRequest {
            model: self.model.clone(),
            system,
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

/// Digest of the run contract: the spec, the tool definitions and the hook
/// policy digest (BLUEPRINT 13.1.2). A run stores it at creation; resume and
/// approve refuse an agent whose contract differs.
pub(crate) fn contract_digest(
    spec: &AgentSpec,
    tools: &[ToolDefinition],
    hooks: Digest,
) -> Result<Digest, CanonicalError> {
    let value = json!({
        "spec": spec,
        "tools": tools,
        "hooks": hooks.to_string(),
    });
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
        let spec = AgentSpec::new("m");
        let base = contract_digest(&spec, &[], hooks).unwrap();
        assert_eq!(base, contract_digest(&spec.clone(), &[], hooks).unwrap());
        let mut other = spec.clone();
        other.system_prompt = "be brief".into();
        assert_ne!(base, contract_digest(&other, &[], hooks).unwrap());
        let tool = ToolDefinition {
            name: "Read".into(),
            description: String::new(),
            input_schema: Default::default(),
        };
        assert_ne!(base, contract_digest(&spec, &[tool], hooks).unwrap());
        let digest = crate::canonical::digest(&json!(1)).unwrap();
        assert_ne!(base, contract_digest(&spec, &[], digest).unwrap());
    }
}
