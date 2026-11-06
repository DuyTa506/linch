//! The registrations one server holds, and the diff applied on relist.

use std::collections::HashMap;
use std::sync::Arc;

use linch_core::{Registration, RegistryError, ToolRegistry};

use crate::tool::{Link, McpTool, ToolSpec};

/// A listed tool that is not in the registry, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkippedTool {
    /// The tool's name on the server.
    pub remote_name: String,
    /// Why the registry refused it: a name over 64 characters, a name taken
    /// by another tool, or an invalid input schema.
    pub reason: RegistryError,
}

struct Entry {
    spec: ToolSpec,
    registration: Registration,
}

/// The tools one server registered, in listing order.
#[derive(Default)]
pub(crate) struct Catalog {
    entries: Vec<Entry>,
    skipped: Vec<SkippedTool>,
}

impl Catalog {
    /// Make the registry match `listed` and touch only what changed.
    ///
    /// An unchanged tool keeps its registration. A changed tool is replaced
    /// first and its old registration disposed after, so the name never
    /// leaves the registry and keeps its position (the prompt cache depends
    /// on it). A tool the server no longer lists is disposed.
    pub(crate) fn sync(
        &mut self,
        registry: &ToolRegistry,
        link: &Arc<Link>,
        listed: Vec<ToolSpec>,
    ) {
        let mut old: HashMap<String, Entry> = self
            .entries
            .drain(..)
            .map(|entry| (entry.spec.name.clone(), entry))
            .collect();
        self.skipped.clear();
        for spec in listed {
            let registered = match old.remove(&spec.name) {
                Some(entry) if entry.spec == spec => Ok(entry),
                Some(entry) => {
                    let replaced = register(registry, link, spec.clone(), true);
                    entry.registration.dispose();
                    replaced
                }
                None => register(registry, link, spec.clone(), false),
            };
            match registered {
                Ok(entry) => self.entries.push(entry),
                Err(reason) => self.skipped.push(SkippedTool {
                    remote_name: spec.remote,
                    reason,
                }),
            }
        }
        for entry in old.into_values() {
            entry.registration.dispose();
        }
    }

    /// Dispose every registration this server made.
    pub(crate) fn clear(&mut self) {
        for entry in self.entries.drain(..) {
            entry.registration.dispose();
        }
    }

    /// Registry names of the registered tools, in listing order.
    pub(crate) fn names(&self) -> Vec<String> {
        self.entries
            .iter()
            .map(|entry| entry.spec.name.clone())
            .collect()
    }

    /// Tools the last listing could not register.
    pub(crate) fn skipped(&self) -> Vec<SkippedTool> {
        self.skipped.clone()
    }
}

fn register(
    registry: &ToolRegistry,
    link: &Arc<Link>,
    spec: ToolSpec,
    replace: bool,
) -> Result<Entry, RegistryError> {
    let tool = Arc::new(McpTool::new(spec.clone(), Arc::clone(link)));
    let registration = if replace {
        registry.replace(tool)
    } else {
        registry.register(tool)
    }?;
    Ok(Entry { spec, registration })
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use linch_core::{JsonObject, ToolScope};
    use tokio::sync::watch;

    use super::*;
    use crate::server::ServerStatus;

    fn link() -> Arc<Link> {
        Arc::new(Link {
            server: "s".into(),
            call_timeout: Duration::from_secs(1),
            stdio: false,
            conn: watch::Sender::new(None),
            status: watch::Sender::new(ServerStatus::Connected),
        })
    }

    fn spec(remote: &str, description: &str) -> ToolSpec {
        let mut input_schema = JsonObject::new();
        input_schema.insert("type".into(), "object".into());
        ToolSpec {
            name: crate::tool::tool_name("s", remote),
            remote: remote.into(),
            description: description.into(),
            input_schema,
            scope: ToolScope::Exec,
        }
    }

    fn visible(registry: &ToolRegistry) -> Vec<(String, String)> {
        registry
            .snapshot()
            .definitions()
            .into_iter()
            .map(|definition| (definition.name, definition.description))
            .collect()
    }

    #[test]
    fn relist_touches_only_what_changed() {
        let registry = ToolRegistry::new();
        let link = link();
        let mut catalog = Catalog::default();
        catalog.sync(
            &registry,
            &link,
            vec![spec("a", "A"), spec("b", "B"), spec("c", "C")],
        );
        let after_first = registry.generation();

        // Same listing: nothing changes, not even the generation.
        catalog.sync(
            &registry,
            &link,
            vec![spec("a", "A"), spec("b", "B"), spec("c", "C")],
        );
        assert_eq!(registry.generation(), after_first);

        // b changes, c goes, d arrives; b keeps its position.
        catalog.sync(
            &registry,
            &link,
            vec![spec("a", "A"), spec("b", "B2"), spec("d", "D")],
        );
        assert_eq!(
            visible(&registry),
            [
                ("mcp__s__a".to_owned(), "A".to_owned()),
                ("mcp__s__b".to_owned(), "B2".to_owned()),
                ("mcp__s__d".to_owned(), "D".to_owned()),
            ]
        );
        assert_eq!(catalog.names(), ["mcp__s__a", "mcp__s__b", "mcp__s__d"]);

        catalog.clear();
        assert!(visible(&registry).is_empty());
        assert!(catalog.names().is_empty());
    }

    #[test]
    fn refused_tools_are_skipped_not_fatal() {
        let registry = ToolRegistry::new();
        let link = link();
        let mut catalog = Catalog::default();
        let long = "x".repeat(60);
        let mut bad_schema = spec("bad", "Bad");
        bad_schema
            .input_schema
            .insert("type".into(), "string".into());
        catalog.sync(
            &registry,
            &link,
            vec![
                spec("ok", "Ok"),
                spec(&long, "Long"),
                bad_schema,
                spec("ok", "Twin"),
            ],
        );
        assert_eq!(catalog.names(), ["mcp__s__ok"]);
        let reasons: Vec<_> = catalog
            .skipped()
            .into_iter()
            .map(|skipped| (skipped.remote_name, skipped.reason))
            .collect();
        assert!(matches!(&reasons[0], (name, RegistryError::InvalidName { .. }) if *name == long));
        assert!(
            matches!(&reasons[1], (name, RegistryError::InputNotObject { .. }) if name == "bad")
        );
        assert!(matches!(&reasons[2], (name, RegistryError::Duplicate { .. }) if name == "ok"));

        // A later listing without the problem clears the skipped list.
        catalog.sync(&registry, &link, vec![spec("ok", "Ok")]);
        assert!(catalog.skipped().is_empty());
    }

    #[test]
    fn a_tool_changed_into_an_invalid_one_is_removed() {
        let registry = ToolRegistry::new();
        let link = link();
        let mut catalog = Catalog::default();
        catalog.sync(&registry, &link, vec![spec("a", "A")]);
        let mut broken = spec("a", "A");
        broken.input_schema.insert("type".into(), "array".into());
        catalog.sync(&registry, &link, vec![broken]);
        assert!(visible(&registry).is_empty());
        assert_eq!(catalog.skipped().len(), 1);
    }

    #[test]
    fn a_draft_07_schema_registers() {
        // The MCP spec shows this as a valid inputSchema.
        let registry = ToolRegistry::new();
        let mut draft_07 = spec("calc", "Add.");
        draft_07.input_schema.insert(
            "$schema".into(),
            "http://json-schema.org/draft-07/schema#".into(),
        );
        draft_07.input_schema.insert(
            "properties".into(),
            serde_json::json!({"a": {"type": "number"}, "b": {"type": "number"}}),
        );
        let mut catalog = Catalog::default();
        catalog.sync(&registry, &link(), vec![draft_07]);
        assert!(catalog.skipped().is_empty(), "{:?}", catalog.skipped());
        assert_eq!(catalog.names(), ["mcp__s__calc"]);
    }
}
