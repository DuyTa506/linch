//! Native catalog changes on the Responses API, from gpt-5.4 on: Python
//! Linch's `native_catalog_changes`.
//!
//! The core appends a tool added mid-session to the request's `tools`,
//! which costs the prompt cache once. With this on, the adapter takes such
//! a tool back out of `tools` and sends it where its catalog notice is, as
//! an `additional_tools` input item after the user text holding the
//! notice. Once any tool was added or removed, the request's `tool_choice`
//! (when the host set none, or `auto`) becomes an `allowed_tools` list of
//! what is still offered, so a removed tool keeps its cached schema but
//! cannot be called. The notice text is still sent, so the model reads
//! every change either way.
//!
//! Off by default ([`ResponsesOptions::native_catalog_changes`]). Not
//! verified against the live API.

use linch_core::{CatalogChangeBlock, JsonValue, Message, ToolDefinition};
use serde_json::json;

use super::ResponsesOptions;
use crate::native;

/// Whether a request for `model` through `options` renders catalog
/// changes natively.
pub(crate) fn enabled(options: &ResponsesOptions, model: &str) -> bool {
    options.native_catalog_changes && supports(model)
}

/// Whether `model` is `gpt-<major>[.<minor>]`, optionally followed by
/// `-<suffix>`, at version 5.4 or later.
fn supports(model: &str) -> bool {
    let Some(rest) = model.strip_prefix("gpt-") else {
        return false;
    };
    let version = rest.split_once('-').map_or(rest, |(version, _)| version);
    let (major, minor) = version.split_once('.').unwrap_or((version, "0"));
    let number = |text: &str| {
        let digits = !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
        digits.then(|| text.parse::<u64>().ok()).flatten()
    };
    matches!((number(major), number(minor)), (Some(major), Some(minor)) if (major, minor) >= (5, 4))
}

/// The catalog of one native request, read from its tools and notices.
pub(crate) struct Native {
    /// The request's tools a notice added by value.
    appended: std::collections::BTreeSet<String>,
    /// Every tool the conversation offered, in order, and whether it
    /// still is.
    offered: Vec<(String, bool)>,
    /// Whether a notice added or removed a tool.
    changed: bool,
}

impl Native {
    /// The catalog of `tools` and the notices of `messages` (see
    /// [`native::appended`]).
    pub(crate) fn new(tools: &[ToolDefinition], messages: &[Message]) -> Self {
        let appended = native::appended(tools, messages);
        let offered = (tools.iter())
            .filter(|tool| !appended.contains(&tool.name))
            .map(|tool| (tool.name.clone(), true))
            .collect();
        Self {
            appended,
            offered,
            changed: false,
        }
    }

    /// Whether `tool` goes in the request's `tools`.
    pub(crate) fn keeps(&self, tool: &ToolDefinition) -> bool {
        !self.appended.contains(&tool.name)
    }

    /// The definitions `change` adds by value, in its order; and what it
    /// adds and removes, noted for [`Self::tool_choice`].
    pub(crate) fn additions<'a>(
        &mut self,
        change: &'a CatalogChangeBlock,
    ) -> Vec<&'a ToolDefinition> {
        let known = |offered: &[(String, bool)], name: &str| offered.iter().any(|(n, _)| n == name);
        let definitions: Vec<&ToolDefinition> = (change.tool_definitions.iter())
            .filter(|tool| self.appended.contains(&tool.name) && !known(&self.offered, &tool.name))
            .collect();
        for name in &change.added_tools {
            match self.offered.iter_mut().find(|(n, _)| n == name) {
                Some((_, on)) => *on = true,
                None if self.appended.contains(name) => self.offered.push((name.clone(), true)),
                None => {}
            }
        }
        for name in &change.removed_tools {
            if let Some((_, on)) = self.offered.iter_mut().find(|(n, _)| n == name) {
                *on = false;
            }
        }
        self.changed |= !change.added_tools.is_empty() || !change.removed_tools.is_empty();
        definitions
    }

    /// The `allowed_tools` choice of what is still offered, once a notice
    /// added or removed a tool.
    pub(crate) fn tool_choice(&self) -> Option<JsonValue> {
        let tools: Vec<JsonValue> = (self.offered.iter())
            .filter(|(_, on)| *on)
            .map(|(name, _)| json!({"type": "function", "name": name}))
            .collect();
        (self.changed).then(|| json!({"type": "allowed_tools", "mode": "auto", "tools": tools}))
    }
}
