//! The system prompt as named sections (Python's `SystemPromptConfig`):
//! [`SystemSection`]s sorted by an order, an `append` text and
//! `replace_defaults`, on [`AgentSpec::system`](crate::AgentSpec::system).
//!
//! The sections are part of the spec, so they are covered by the run
//! contract: a run resumed with another set is refused like any other
//! contract mismatch. A spec that sets none keeps the digest it always had.
//!
//! The system of a request is, in order: the sections by `order` (ties in
//! declaration order), the spec's `system_blocks`, its `system_prompt`, the
//! skill listing when the agent has skills, then `append`. Every block is
//! sent as its own provider system block; the section's name is for
//! operators and is never sent.

use serde::{Deserialize, Serialize};

use crate::agent::AgentSpec;
use crate::message::SystemBlock;

fn yes() -> bool {
    true
}

fn is_true(value: &bool) -> bool {
    *value
}

fn is_zero(value: &i32) -> bool {
    *value == 0
}

/// One named layer of the system prompt, sent as one system block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SystemSection {
    /// A name for operators and diagnostics, never sent to the model. It is
    /// unique among the sections and not blank.
    pub name: String,
    /// The text of the block; not blank.
    pub text: String,
    /// Where the section goes among the others: lowest first, a tie in the
    /// order the sections are declared. Default 0.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub order: i32,
    /// Whether the prompt prefix ending at this block may be cached
    /// (default true). A provider that takes one breakpoint puts it on the
    /// last cacheable block, so everything before it is cached too: a
    /// section that is not cacheable may not come before a cacheable block
    /// ([`SystemConfig::problem`]).
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub cacheable: bool,
}

impl SystemSection {
    /// A cacheable section with order 0.
    pub fn new(name: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            text: text.into(),
            order: 0,
            cacheable: true,
        }
    }

    /// This section at `order`.
    pub fn order(mut self, order: i32) -> Self {
        self.order = order;
        self
    }

    /// This section with its `cacheable` flag set to `cacheable`.
    pub fn cacheable(mut self, cacheable: bool) -> Self {
        self.cacheable = cacheable;
        self
    }
}

/// How the system prompt is built beyond the flat `system_prompt` and
/// `system_blocks`. The default changes nothing and stays off the wire.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SystemConfig {
    /// The named sections, sent first, sorted by their `order`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sections: Vec<SystemSection>,
    /// Text sent as the last system block, after the system prompt (and
    /// whatever a preset added to it) and the skill listing. Not cacheable,
    /// like the system prompt. Python's `append`, which there replaces the
    /// prompt; here it is added to it, so a preset's text is not lost.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub append: Option<String>,
    /// Whether the presets' own prompt text (the deep agent's operating
    /// instructions, the filesystem tools' notes, a profile's prompt) is
    /// left out of `system_prompt`, so the host's text stands alone.
    /// Python's `replace_defaults`; core has no defaults of its own, the
    /// facade's presets are the defaults here. Operator sections a server
    /// adds are not defaults and stay.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub replace_defaults: bool,
}

impl SystemConfig {
    /// Whether this changes nothing, which a spec leaves off the wire.
    pub fn is_default(&self) -> bool {
        *self == Self::default()
    }

    /// The sections in the order they are sent: by `order`, a tie in the
    /// order declared.
    pub fn sorted(&self) -> Vec<&SystemSection> {
        let mut sections: Vec<_> = self.sections.iter().collect();
        sections.sort_by_key(|section| section.order);
        sections
    }

    /// Why these sections cannot be sent ahead of `host`, the spec's own
    /// system blocks, if they cannot: a blank or repeated name, a blank
    /// text or `append`, or a section that is not cacheable before a block
    /// that is.
    pub fn problem(&self, host: &[SystemBlock]) -> Option<String> {
        let mut names = std::collections::HashSet::new();
        for section in &self.sections {
            if section.name.trim().is_empty() {
                return Some("a system section name must not be blank".into());
            }
            if section.text.trim().is_empty() {
                return Some(format!(
                    "system section `{}` must not have blank text",
                    section.name
                ));
            }
            if !names.insert(section.name.as_str()) {
                return Some(format!(
                    "system section `{}` is declared twice",
                    section.name
                ));
            }
        }
        if self
            .append
            .as_deref()
            .is_some_and(|text| text.trim().is_empty())
        {
            return Some("system.append must not be blank".into());
        }
        let sorted = self.sorted();
        for (at, section) in sorted.iter().enumerate() {
            let cached_after = sorted[at + 1..].iter().any(|later| later.cacheable)
                || host.iter().any(|block| block.cacheable);
            if !section.cacheable && cached_after {
                return Some(format!(
                    "system section `{}` is not cacheable but a cacheable block follows it, \
                     so a provider's cache breakpoint would cache it too; order it after the \
                     cacheable blocks",
                    section.name
                ));
            }
        }
        None
    }
}

/// The blocks of a request's system, and where the skill listing goes.
pub(crate) struct Layout {
    /// Every block, in the order sent.
    pub blocks: Vec<SystemBlock>,
    /// Index at which a skill listing is inserted: after the system prompt,
    /// before `append`.
    pub listing_at: usize,
}

/// The system of `spec`: its sections, `system_blocks`, `system_prompt`
/// and `append`, in that order.
pub(crate) fn layout(spec: &AgentSpec) -> Layout {
    let mut blocks: Vec<SystemBlock> = (spec.system.sorted().into_iter())
        .map(|section| SystemBlock {
            text: section.text.clone(),
            cacheable: section.cacheable,
        })
        .collect();
    blocks.extend(spec.system_blocks.iter().cloned());
    if !spec.system_prompt.is_empty() {
        blocks.push(SystemBlock {
            text: spec.system_prompt.clone(),
            cacheable: false,
        });
    }
    let listing_at = blocks.len();
    if let Some(text) = &spec.system.append {
        blocks.push(SystemBlock {
            text: text.clone(),
            cacheable: false,
        });
    }
    Layout { blocks, listing_at }
}

#[cfg(test)]
mod tests;
