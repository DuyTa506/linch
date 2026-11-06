//! What the adapters that render catalog changes natively share: which
//! of a request's tools a catalog notice added by value.
//!
//! The core keeps a session's tools in append-only form: the frozen
//! prefix's rows, then each tool added later (see `linch_core::catalog`).
//! An adapter that can add a tool in the conversation takes the later
//! ones back out of `tools` and adds each where its notice is, so the
//! cached prefix keeps its bytes.

use std::collections::{BTreeMap, BTreeSet};

use linch_core::{CatalogChangeBlock, ContentBlock, Message, ToolDefinition};

/// The tools of `tools` a notice of `messages` added by value: those
/// whose first mention in a notice adds them, with the definition their
/// row still has. A prefix tool is first mentioned by its removal, and a
/// tool whose schema changed since has a new row; both stay in `tools`.
pub(crate) fn appended(tools: &[ToolDefinition], messages: &[Message]) -> BTreeSet<String> {
    let mut first: BTreeMap<&str, Option<&ToolDefinition>> = BTreeMap::new();
    for change in notices(messages) {
        for name in &change.added_tools {
            let definition = (change.tool_definitions.iter()).find(|d| d.name == *name);
            first.entry(name).or_insert(definition);
        }
        for name in &change.removed_tools {
            first.entry(name).or_insert(None);
        }
        for definition in &change.tool_definitions {
            first.entry(&definition.name).or_insert(None);
        }
    }
    (tools.iter())
        .filter(|tool| first.get(tool.name.as_str()) == Some(&Some(*tool)))
        .map(|tool| tool.name.clone())
        .collect()
}

/// The catalog notices of `messages`, in order.
pub(crate) fn notices(messages: &[Message]) -> impl Iterator<Item = &CatalogChangeBlock> {
    (messages.iter().flat_map(|m| &m.content)).filter_map(|block| match block {
        ContentBlock::CatalogChange(change) => Some(change),
        _ => None,
    })
}
