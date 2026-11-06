//! Tool registry: registration, generations and input validation.
//!
//! A [`ToolRegistry`] is shared across runs. Each turn takes one
//! [`ToolSnapshot`] and uses it throughout, so a registration that lands
//! mid-turn never changes the tools, schemas or order that turn sees.
//!
//! Registration compiles a tool's schemas once ([`RegisteredTool`]). The
//! returned [`Registration`] undoes exactly that call: disposing a handle whose
//! tool was replaced removes it from the replacement chain without touching
//! the tool that is visible now.

mod schema;
#[cfg(test)]
mod tests;

pub use schema::{INVALID_INPUT_CODE, OutputContractError, RegisteredTool, SchemaKind};

use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use thiserror::Error;

use crate::provider::ToolDefinition;
use crate::registration::Registration;
use crate::tool::{Tool, ToolScope};

/// Longest tool name accepted by every supported provider.
const MAX_NAME_LEN: usize = 64;

/// Why a tool could not be registered.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RegistryError {
    /// The name is not 1-64 ASCII letters, digits, `_` or `-`.
    #[error("tool name {name:?} must be 1-64 ASCII letters, digits, '_' or '-'")]
    InvalidName {
        /// The rejected name.
        name: String,
    },
    /// [`ToolRegistry::register`] found a tool with the same name.
    #[error("tool {name:?} is already registered")]
    Duplicate {
        /// The name already in use.
        name: String,
    },
    /// The input schema does not declare `"type": "object"` at the top level.
    #[error("tool {name:?} input schema must declare \"type\": \"object\" at the top level")]
    InputNotObject {
        /// The tool's name.
        name: String,
    },
    /// A schema is not valid JSON Schema draft 2020-12, or a `$ref` in it
    /// cannot be resolved without I/O.
    #[error("tool {name:?} {kind} schema is invalid: {message}")]
    InvalidSchema {
        /// The tool's name.
        name: String,
        /// Which schema failed.
        kind: SchemaKind,
        /// Where in the schema the problem is, and what it is.
        message: String,
    },
}

/// Named tools, shared across runs behind an `Arc`.
///
/// The [`generation`](Self::generation) counter bumps on every change to the
/// visible tool set: a register, a replace, or disposing the registration that
/// is currently visible. Disposing a registration that a later replace already
/// hides changes nothing visible and does not bump it.
#[derive(Default)]
pub struct ToolRegistry {
    inner: Arc<Mutex<Inner>>,
}

impl ToolRegistry {
    /// An empty registry at generation 0.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a tool under a name not yet in use.
    ///
    /// Fails on an invalid name, an invalid schema, or a duplicate name. The
    /// returned handle removes this tool again.
    pub fn register(&self, tool: Arc<dyn Tool>) -> Result<Registration, RegistryError> {
        self.add(tool, false)
    }

    /// Add a tool, hiding any tool registered under the same name.
    ///
    /// A replaced tool keeps its position in [`ToolSnapshot::definitions`].
    /// Disposing the returned handle brings back the most recent registration
    /// under that name that is still live, or removes the name if none is.
    pub fn replace(&self, tool: Arc<dyn Tool>) -> Result<Registration, RegistryError> {
        self.add(tool, true)
    }

    /// Current generation.
    pub fn generation(&self) -> u64 {
        lock(&self.inner).generation
    }

    /// Immutable view of the current tools, for one turn.
    ///
    /// Calls between two changes return the same `Arc`.
    pub fn snapshot(&self) -> Arc<ToolSnapshot> {
        let mut inner = lock(&self.inner);
        if let Some(snapshot) = &inner.snapshot {
            return Arc::clone(snapshot);
        }
        let snapshot = Arc::new(ToolSnapshot::new(&inner));
        inner.snapshot = Some(Arc::clone(&snapshot));
        snapshot
    }

    fn add(&self, tool: Arc<dyn Tool>, replace: bool) -> Result<Registration, RegistryError> {
        if !is_valid_name(tool.name()) {
            return Err(RegistryError::InvalidName {
                name: tool.name().to_owned(),
            });
        }
        // Compile outside the lock: it runs tool code and may be slow.
        let entry = Arc::new(RegisteredTool::compile(tool)?);
        let name = entry.name().to_owned();
        let mut guard = lock(&self.inner);
        let inner = &mut *guard;
        let id = inner.next_id;
        match inner.slots.iter_mut().find(|slot| slot.name == name) {
            Some(_) if !replace => return Err(RegistryError::Duplicate { name }),
            Some(slot) => slot.stack.push((id, entry)),
            None => inner.slots.push(Slot {
                name: name.clone(),
                stack: vec![(id, entry)],
            }),
        }
        inner.next_id += 1;
        inner.bump();
        drop(guard);

        let registry = Arc::downgrade(&self.inner);
        Ok(Registration::new(move || {
            if let Some(inner) = registry.upgrade() {
                // Bind the removed tool so its last `Arc` drops after the
                // lock is released, in case its `Drop` touches the registry.
                let removed = lock(&inner).remove(&name, id);
                drop(removed);
            }
        }))
    }
}

/// The tools one turn uses, fixed when the snapshot was taken.
pub struct ToolSnapshot {
    generation: u64,
    tools: Vec<Arc<RegisteredTool>>,
    index: HashMap<String, usize>,
}

impl ToolSnapshot {
    fn new(inner: &Inner) -> Self {
        let tools: Vec<_> = inner
            .slots
            .iter()
            .filter_map(|slot| slot.stack.last().map(|(_, tool)| Arc::clone(tool)))
            .collect();
        let index = tools
            .iter()
            .enumerate()
            .map(|(position, tool)| (tool.name().to_owned(), position))
            .collect();
        Self {
            generation: inner.generation,
            tools,
            index,
        }
    }

    /// Registry generation this snapshot was taken at.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// The tool registered under `name`, if any.
    pub fn resolve(&self, name: &str) -> Option<&Arc<RegisteredTool>> {
        self.index.get(name).map(|&position| &self.tools[position])
    }

    /// Tool definitions for the provider request, in registration order.
    ///
    /// The order is stable across snapshots: the prompt cache depends on it.
    pub fn definitions(&self) -> Vec<ToolDefinition> {
        self.tools
            .iter()
            .map(|tool| tool.definition().clone())
            .collect()
    }

    /// Each tool's definition with its scope, in registration order: the
    /// tool part of the run contract. The scope decides approval and
    /// parallelism, so it counts as much as the schema.
    pub(crate) fn contract_tools(&self) -> Vec<(ToolDefinition, ToolScope)> {
        (self.tools.iter())
            .map(|tool| (tool.definition().clone(), tool.tool().scope()))
            .collect()
    }
}

impl fmt::Debug for ToolSnapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ToolSnapshot")
            .field("generation", &self.generation)
            .field("tools", &self.tools)
            .finish()
    }
}

#[derive(Default)]
struct Inner {
    /// One slot per visible name, in first-registration order.
    slots: Vec<Slot>,
    generation: u64,
    next_id: u64,
    /// Snapshot of the current generation, built on first request.
    snapshot: Option<Arc<ToolSnapshot>>,
}

struct Slot {
    name: String,
    /// Live registrations under this name, oldest first; the last is visible.
    stack: Vec<(u64, Arc<RegisteredTool>)>,
}

impl Inner {
    fn bump(&mut self) {
        self.generation += 1;
        self.snapshot = None;
    }

    /// Remove registration `id` under `name`; bump only if it was visible.
    fn remove(&mut self, name: &str, id: u64) -> Option<Arc<RegisteredTool>> {
        let index = self.slots.iter().position(|slot| slot.name == name)?;
        let stack = &mut self.slots[index].stack;
        let position = stack.iter().position(|(entry, _)| *entry == id)?;
        let visible = position + 1 == stack.len();
        let (_, removed) = stack.remove(position);
        if stack.is_empty() {
            self.slots.remove(index);
        }
        if visible {
            self.bump();
        }
        Some(removed)
    }
}

fn lock(inner: &Mutex<Inner>) -> MutexGuard<'_, Inner> {
    inner.lock().unwrap_or_else(PoisonError::into_inner)
}

/// 1-64 ASCII letters, digits, `_` or `-`: the intersection of what the
/// Anthropic, OpenAI and Gemini tool-name rules accept.
fn is_valid_name(name: &str) -> bool {
    (1..=MAX_NAME_LEN).contains(&name.len())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}
