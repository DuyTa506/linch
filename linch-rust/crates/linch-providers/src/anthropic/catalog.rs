//! Native catalog changes on the Messages API, behind the beta
//! [`BETA`]: Python Linch's `native_catalog_changes`.
//!
//! The core appends a tool added mid-session to the request's `tools`,
//! which costs the prompt cache once. With this on, the adapter takes such
//! a tool back out of `tools` and adds it where its catalog notice is: a
//! `system`-role message of tool blocks after the message holding the
//! notice, with a `tool_addition` of a `tool_definition`. A tool of the
//! session's prefix that is removed is a `tool_removal` of a
//! `tool_reference`, and adding it back a `tool_addition` of one. A tool
//! added by value and removed again stays: only text tells the model, and
//! the runner refuses its calls. The notice text still goes in the user
//! message, so the model reads every change either way.
//!
//! Off by default ([`AnthropicOptions::native_catalog_changes`]), and only
//! on Anthropic's own host with the models in [`MODELS`]. Not verified
//! against the live API.

use std::collections::BTreeSet;

use linch_core::{CatalogChangeBlock, JsonValue, Message, ProviderError, ToolDefinition};
use serde_json::json;

use super::AnthropicOptions;
use crate::native;

/// The `anthropic-beta` value a native request sends.
pub(crate) const BETA: &str = "inline-tools-2026-09-15";

/// The models that take tool blocks in the conversation, as Python Linch
/// lists them.
pub(crate) const MODELS: [&str; 9] = [
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-opus-4-8",
];

/// The most bytes of tool definitions one request may add natively.
const MAX_DEFINITION_BYTES: usize = 4_194_304;

/// Whether a request for `model` through `options` renders catalog
/// changes natively.
pub(crate) fn enabled(options: &AnthropicOptions, model: &str) -> bool {
    let host = reqwest::Url::parse(&options.base_url).ok();
    let on_anthropic = host.is_some_and(|url| url.host_str() == Some("api.anthropic.com"));
    options.native_catalog_changes && on_anthropic && MODELS.contains(&model)
}

/// The catalog of one native request, read from its tools and notices.
pub(crate) struct Native {
    /// The request's tools a notice added by value, which the adapter
    /// sends at the notice instead of in `tools`.
    appended: BTreeSet<String>,
    /// The request's other tools: the session's prefix as the model has
    /// it, which a reference may name.
    prefix: BTreeSet<String>,
    /// The tools the conversation has offered so far.
    active: BTreeSet<String>,
    /// The definition bytes added so far.
    bytes: usize,
}

impl Native {
    /// The catalog of `tools` and the notices of `messages` (see
    /// [`native::appended`]).
    pub(crate) fn new(tools: &[ToolDefinition], messages: &[Message]) -> Self {
        let appended = native::appended(tools, messages);
        let prefix: BTreeSet<String> = (tools.iter())
            .filter(|tool| !appended.contains(&tool.name))
            .map(|tool| tool.name.clone())
            .collect();
        Self {
            appended,
            active: prefix.clone(),
            prefix,
            bytes: 0,
        }
    }

    /// Whether `tool` goes in the request's `tools`.
    pub(crate) fn keeps(&self, tool: &ToolDefinition) -> bool {
        !self.appended.contains(&tool.name)
    }

    /// The tool blocks of `change`, in Python Linch's order: additions,
    /// then removals.
    pub(crate) fn blocks(
        &mut self,
        change: &CatalogChangeBlock,
        wire: impl Fn(&ToolDefinition) -> JsonValue,
    ) -> Result<Vec<JsonValue>, ProviderError> {
        let mut out = Vec::new();
        for name in &change.added_tools {
            if self.active.contains(name) {
                continue;
            }
            let definition = (change.tool_definitions.iter()).find(|d| d.name == *name);
            let tool = match definition {
                _ if self.prefix.contains(name) => reference(name),
                Some(definition) if self.appended.contains(name) => {
                    let definition = wire(definition);
                    self.bytes += serde_json::to_vec(&definition).map_or(0, |bytes| bytes.len());
                    if self.bytes > MAX_DEFINITION_BYTES {
                        return Err(ProviderError::InvalidRequest {
                            message: "Native tool definitions exceed the 4,194,304-byte limit; \
                                      start a new session or disable native_catalog_changes."
                                .into(),
                        });
                    }
                    json!({"type": "tool_definition", "definition": definition})
                }
                _ => continue,
            };
            out.push(json!({"type": "tool_addition", "tool": tool}));
            self.active.insert(name.clone());
        }
        for name in &change.removed_tools {
            if self.prefix.contains(name) && self.active.remove(name) {
                out.push(json!({"type": "tool_removal", "tool": reference(name)}));
            }
        }
        Ok(out)
    }
}

fn reference(name: &str) -> JsonValue {
    json!({"type": "tool_reference", "name": name})
}
