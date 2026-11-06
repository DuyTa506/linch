//! What the adapters that render catalog changes natively share: which
//! of a request's tools the session added after its frozen prefix.
//!
//! The core keeps a session's tools in append-only form: the frozen
//! prefix's rows, then each tool added later, which it names in
//! [`ProviderRequest::appended_tools`] (see `linch_core::catalog`). An
//! adapter that can add a tool in the conversation takes those back out
//! of `tools` and adds each where its notice is, so the cached prefix
//! keeps its bytes. The adapter does not work them out from the notices:
//! a compaction folds the notices into one restatement.

use std::collections::BTreeMap;

use linch_core::{ProviderRequest, ToolDefinition};

/// The tools of `request` added after the session's prefix, by name, with
/// the definition their row has.
pub(crate) fn appended(request: &ProviderRequest) -> BTreeMap<String, ToolDefinition> {
    (request.tools.iter())
        .filter(|tool| request.appended_tools.contains(&tool.name))
        .map(|tool| (tool.name.clone(), tool.clone()))
        .collect()
}

#[cfg(test)]
mod tests;
