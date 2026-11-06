//! [`Profile`]: a versioned role for an agent's runs, as data (Python
//! Linch's domain profiles).

use linch_core::permissions::{RuleDecision, ToolRule};
use linch_core::{AgentSpec, JsonValue, PermissionLayer, digest_with_domain};
use serde::{Deserialize, Serialize};
use serde_json::json;
use thiserror::Error;

/// Domain separator for [`Profile::content_hash`].
const HASH_DOMAIN: &str = "linch.profile";

/// A versioned role and the run defaults that go with it: instructions
/// added after the agent's own, tools it may not call, and a turn limit.
///
/// Support and billing on one store are two agents opened from configs
/// that differ only in `profile` (with one [`PgPools`](crate::PgPools),
/// they share the pool). Opening applies the profile (see
/// [`AgentConfig::profile`](crate::AgentConfig::profile)); [`Self::spec`]
/// and [`Self::deny_layer`] do the same for an agent built with
/// [`AgentBuilder`](linch_core::AgentBuilder).
///
/// Python selects a profile per run on one agent; here it is chosen when
/// the agent opens. The run contract covers what the profile changes in
/// the spec, but not its id, version or denied tools.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    /// Stable domain or tenant profile identifier.
    pub id: String,
    /// Revision of the profile.
    pub version: String,
    /// Role instructions, added after the agent's system prompt.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt: Option<String>,
    /// Tools a call to which is always denied, whatever the permission
    /// mode or other rules say. They are still offered to the model.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_deny: Option<Vec<String>>,
    /// Most provider calls one run may make; replaces the spec's.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_turns: Option<u32>,
}

/// A [`Profile`] that cannot be used as written.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ProfileError {
    /// A field is blank or out of range.
    #[error("profile {field} {problem}")]
    Invalid {
        /// The field at fault.
        field: &'static str,
        /// What is wrong with it.
        problem: &'static str,
    },
}

impl Profile {
    /// A profile `id` at `version` that changes nothing else.
    pub fn new(id: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            version: version.into(),
            system_prompt: None,
            tool_deny: None,
            max_turns: None,
        }
    }

    /// Check that the id and version are not blank, `max_turns` is at
    /// least 1 and no denied tool name is blank.
    ///
    /// Raises:
    ///     ProfileError: `Invalid`, naming the first field at fault.
    pub fn validate(&self) -> Result<(), ProfileError> {
        let invalid = |field, problem| Err(ProfileError::Invalid { field, problem });
        if self.id.trim().is_empty() {
            return invalid("id", "must not be blank");
        }
        if self.version.trim().is_empty() {
            return invalid("version", "must not be blank");
        }
        if self.max_turns == Some(0) {
            return invalid("max_turns", "must be at least 1");
        }
        let names = self.tool_deny.iter().flatten();
        if names.into_iter().any(|name| name.trim().is_empty()) {
            return invalid("tool_deny", "must hold non-blank tool names");
        }
        Ok(())
    }

    /// This profile with `other` layered on top: each field `other` sets
    /// replaces this one's (an empty `tool_deny` replaces a list too), and
    /// the id and version are `other`'s.
    pub fn layer(&self, other: &Profile) -> Profile {
        Profile {
            id: other.id.clone(),
            version: other.version.clone(),
            system_prompt: (other.system_prompt.clone()).or_else(|| self.system_prompt.clone()),
            tool_deny: (other.tool_deny.clone()).or_else(|| self.tool_deny.clone()),
            max_turns: other.max_turns.or(self.max_turns),
        }
    }

    /// Every field as JSON, unset ones as `null`, for inspection and
    /// configuration dumps.
    pub fn resolve(&self) -> JsonValue {
        json!({
            "id": self.id,
            "version": self.version,
            "system_prompt": self.system_prompt,
            "tool_deny": self.tool_deny,
            "max_turns": self.max_turns,
        })
    }

    /// `sha256:` and the hex digest of [`Self::resolve`], so two profiles
    /// with the same content, id and version have the same hash.
    pub fn content_hash(&self) -> String {
        let digest = digest_with_domain(HASH_DOMAIN, &self.resolve())
            .expect("a profile is far below the depth limit");
        format!("sha256:{digest}")
    }

    /// `{id, version, content_hash}`: what a host records to name exactly
    /// this profile.
    pub fn reference(&self) -> JsonValue {
        json!({"id": self.id, "version": self.version, "content_hash": self.content_hash()})
    }

    /// `spec` under this profile: its system prompt, then a blank line and
    /// the profile's when it has one, and the profile's `max_turns` when
    /// set.
    pub fn spec(&self, mut spec: AgentSpec) -> AgentSpec {
        if let Some(prompt) = self.system_prompt.as_deref().filter(|p| !p.is_empty()) {
            spec.system_prompt = match spec.system_prompt.is_empty() {
                true => prompt.to_owned(),
                false => format!("{}\n\n{prompt}", spec.system_prompt),
            };
        }
        spec.max_turns = self.max_turns.or(spec.max_turns);
        spec
    }

    /// The permission layer `profile` that denies each of `tool_deny`, in
    /// name order; `None` when it denies nothing. A deny in any layer
    /// wins, so the host's rules cannot let a denied tool run.
    pub fn deny_layer(&self) -> Option<PermissionLayer> {
        let mut names = self.tool_deny.clone().unwrap_or_default();
        names.sort();
        names.dedup();
        let rules = names
            .into_iter()
            .map(|name| ToolRule::new(name, RuleDecision::Deny));
        let layer = PermissionLayer::new("profile", rules.map(Into::into));
        (!layer.rules.is_empty()).then_some(layer)
    }
}
