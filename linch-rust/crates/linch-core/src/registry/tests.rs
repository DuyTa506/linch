//! Registry behaviour; schema checks are tested in `schema.rs`.

use std::sync::Arc;

use async_trait::async_trait;
use proptest::prelude::*;
use serde_json::json;

use super::*;
use crate::json::{JsonObject, JsonValue};
use crate::tool::{ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

struct TestTool {
    name: String,
    description: String,
    input: JsonObject,
    output: Option<JsonObject>,
}

#[async_trait]
impl Tool for TestTool {
    fn name(&self) -> &str {
        &self.name
    }
    fn description(&self) -> &str {
        &self.description
    }
    fn input_schema(&self) -> &JsonObject {
        &self.input
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    fn output_schema(&self) -> Option<&JsonObject> {
        self.output.as_ref()
    }
    /// Rejects `count > 10` and fills a default `mode`, to show it runs
    /// after the schema check and its result is what the caller gets.
    fn validate(&self, mut input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if input.get("count").and_then(JsonValue::as_i64) > Some(10) {
            return Err(ToolOutputError::new("count must be at most 10"));
        }
        input.entry("mode").or_insert_with(|| json!("fast"));
        Ok(input)
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new(JsonValue::Object(input)))
    }
}

pub(super) fn object(value: JsonValue) -> JsonObject {
    match value {
        JsonValue::Object(map) => map,
        other => panic!("not an object: {other}"),
    }
}

pub(super) fn schema_tool(
    name: &str,
    input: JsonValue,
    output: Option<JsonValue>,
) -> Arc<dyn Tool> {
    Arc::new(TestTool {
        name: name.to_owned(),
        description: format!("{name} tool"),
        input: object(input),
        output: output.map(object),
    })
}

/// A tool whose description tells instances with the same name apart.
fn tool(name: &str, tag: &str) -> Arc<dyn Tool> {
    Arc::new(TestTool {
        name: name.to_owned(),
        description: tag.to_owned(),
        input: object(json!({"type": "object"})),
        output: None,
    })
}

fn names(registry: &ToolRegistry) -> Vec<String> {
    let snapshot = registry.snapshot();
    snapshot.definitions().into_iter().map(|d| d.name).collect()
}

fn tag(registry: &ToolRegistry, name: &str) -> Option<String> {
    let snapshot = registry.snapshot();
    snapshot
        .resolve(name)
        .map(|entry| entry.definition().description.clone())
}

#[test]
fn a_failed_registration_changes_nothing() {
    let registry = ToolRegistry::new();
    let _first = registry.register(tool("echo", "v1")).unwrap();
    let error = registry.register(tool("echo", "v2")).unwrap_err();
    assert_eq!(
        error,
        RegistryError::Duplicate {
            name: "echo".into()
        }
    );
    let invalid = registry.replace(schema_tool("echo", json!({}), None));
    assert!(matches!(invalid, Err(RegistryError::InputNotObject { .. })));
    assert_eq!(registry.generation(), 1);
    assert_eq!(tag(&registry, "echo").as_deref(), Some("v1"));
}

#[test]
fn names_follow_the_provider_pattern() {
    let registry = ToolRegistry::new();
    let long = "a".repeat(MAX_NAME_LEN);
    for good in ["a", "Read", "mcp__server__tool-1", long.as_str()] {
        assert!(registry.register(tool(good, "")).is_ok(), "{good}");
    }
    let too_long = "a".repeat(MAX_NAME_LEN + 1);
    for bad in ["", "has space", "dotted.name", "naïve", too_long.as_str()] {
        assert_eq!(
            registry.register(tool(bad, "")).unwrap_err(),
            RegistryError::InvalidName { name: bad.into() }
        );
    }
}

#[test]
fn replace_keeps_position_and_dispose_restores_the_previous_tool() {
    let registry = ToolRegistry::new();
    let _a = registry.register(tool("a", "a1")).unwrap();
    let _b = registry.register(tool("b", "b1")).unwrap();
    let a2 = registry.replace(tool("a", "a2")).unwrap();
    assert_eq!(names(&registry), ["a", "b"]);
    assert_eq!(tag(&registry, "a").as_deref(), Some("a2"));
    a2.dispose();
    assert_eq!(names(&registry), ["a", "b"]);
    assert_eq!(tag(&registry, "a").as_deref(), Some("a1"));
    assert_eq!(registry.generation(), 4);
}

#[test]
fn replace_of_an_unknown_name_registers_it() {
    let registry = ToolRegistry::new();
    let handle = registry.replace(tool("fresh", "x")).unwrap();
    assert_eq!(names(&registry), ["fresh"]);
    handle.dispose();
    assert!(names(&registry).is_empty());
}

#[test]
fn disposing_a_hidden_handle_leaves_the_replacement() {
    let registry = ToolRegistry::new();
    let old = registry.register(tool("a", "old")).unwrap();
    let new = registry.replace(tool("a", "new")).unwrap();
    let generation = registry.generation();
    old.dispose();
    assert_eq!(tag(&registry, "a").as_deref(), Some("new"));
    assert_eq!(registry.generation(), generation, "nothing visible changed");
    // The disposed original is not restored when the replacement goes.
    new.dispose();
    assert_eq!(tag(&registry, "a"), None);
}

#[test]
fn dispose_is_idempotent_and_never_removes_a_later_registration() {
    let registry = ToolRegistry::new();
    let first = registry.register(tool("a", "first")).unwrap();
    first.dispose();
    first.dispose();
    assert_eq!(registry.generation(), 2);
    let _second = registry.register(tool("a", "second")).unwrap();
    first.dispose();
    assert_eq!(tag(&registry, "a").as_deref(), Some("second"));
    assert_eq!(registry.generation(), 3);
}

#[test]
fn a_name_registered_again_moves_to_the_end() {
    let registry = ToolRegistry::new();
    let a = registry.register(tool("a", "")).unwrap();
    let _b = registry.register(tool("b", "")).unwrap();
    a.dispose();
    let _a = registry.register(tool("a", "")).unwrap();
    assert_eq!(names(&registry), ["b", "a"]);
}

#[test]
fn dispose_after_the_registry_is_gone_is_a_no_op() {
    let registry = ToolRegistry::new();
    let handle = registry.register(tool("a", "")).unwrap();
    drop(registry);
    handle.dispose();
    assert!(handle.is_disposed());
}

#[test]
fn snapshot_is_stable_while_the_registry_changes() {
    let registry = ToolRegistry::new();
    let a = registry.register(tool("a", "a1")).unwrap();
    let held = registry.snapshot();
    assert!(Arc::ptr_eq(&held, &registry.snapshot()));

    let _b = registry.register(tool("b", "b1")).unwrap();
    let _a2 = registry.replace(tool("a", "a2")).unwrap();
    a.dispose();

    assert_eq!(held.generation(), 1);
    assert_eq!(held.definitions().len(), 1);
    assert_eq!(held.resolve("a").unwrap().definition().description, "a1");
    assert!(held.resolve("b").is_none());

    let current = registry.snapshot();
    assert!(!Arc::ptr_eq(&held, &current));
    assert_eq!(current.generation(), registry.generation());
    assert_eq!(current.resolve("a").unwrap().definition().description, "a2");
}

#[test]
fn shared_types_are_send_and_sync() {
    fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<ToolRegistry>();
    assert_send_sync::<ToolSnapshot>();
    assert_send_sync::<RegisteredTool>();
}

#[derive(Debug, Clone)]
enum Op {
    Register(usize),
    Replace(usize),
    Dispose(usize),
}

fn op() -> impl Strategy<Value = Op> {
    prop_oneof![
        (0..4usize).prop_map(Op::Register),
        (0..4usize).prop_map(Op::Replace),
        (0..16usize).prop_map(Op::Dispose),
    ]
}

proptest! {
    /// Reference model: a list of (name, live handle ids) in first
    /// registration order; the last live handle of a name is visible.
    #[test]
    fn definitions_follow_registration_order(ops in proptest::collection::vec(op(), 0..40)) {
        let registry = ToolRegistry::new();
        let mut handles: Vec<Registration> = Vec::new();
        let mut model: Vec<(String, Vec<usize>)> = Vec::new();
        let mut generation = 0;
        for op in ops {
            match op {
                Op::Register(n) | Op::Replace(n) => {
                    let name = format!("t{n}");
                    let id = handles.len();
                    let result = match op {
                        Op::Register(_) => registry.register(tool(&name, &id.to_string())),
                        _ => registry.replace(tool(&name, &id.to_string())),
                    };
                    let slot = model.iter_mut().find(|(n, _)| *n == name);
                    match (slot, result) {
                        (Some(_), Err(RegistryError::Duplicate { .. })) => {
                            prop_assert!(matches!(op, Op::Register(_)));
                            continue;
                        }
                        (Some((_, ids)), Ok(handle)) => {
                            ids.push(id);
                            handles.push(handle);
                        }
                        (None, Ok(handle)) => {
                            model.push((name, vec![id]));
                            handles.push(handle);
                        }
                        (_, Err(error)) => prop_assert!(false, "unexpected {error}"),
                    }
                    generation += 1;
                }
                Op::Dispose(i) => {
                    let Some(handle) = handles.get(i) else { continue };
                    handle.dispose();
                    let Some(index) = model.iter().position(|(_, ids)| ids.contains(&i)) else {
                        continue;
                    };
                    let ids = &mut model[index].1;
                    if ids.last() == Some(&i) {
                        generation += 1;
                    }
                    ids.retain(|&id| id != i);
                    if ids.is_empty() {
                        model.remove(index);
                    }
                }
            }
            let snapshot = registry.snapshot();
            let expected: Vec<&str> = model.iter().map(|(name, _)| name.as_str()).collect();
            let actual: Vec<String> = snapshot.definitions().into_iter().map(|d| d.name).collect();
            prop_assert_eq!(&actual, &expected);
            for (name, ids) in &model {
                let visible = snapshot.resolve(name).map(|e| e.definition().description.clone());
                prop_assert_eq!(visible, ids.last().map(ToString::to_string));
            }
            prop_assert_eq!(snapshot.generation(), generation);
        }
    }
}
