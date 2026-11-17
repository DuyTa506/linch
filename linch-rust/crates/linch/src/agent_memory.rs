//! The long-term memory a [`MemoryConfig`] turns on: the three tools and
//! the optional recall hook over one store.

use std::sync::Arc;

use linch_core::{Hooks, InMemoryMemoryStore, MemoryStore, Registration, ToolRegistry};
use linch_postgres::{PgMemoryStore, PgPool};
use linch_tools::{MAX_RECALL, MemoryRecall, memory_tools};

use crate::config::MemoryConfig;
use crate::error::OpenError;

/// What [`Memory::wire`] added to the registry and the hooks.
///
/// Dropping it undoes those registrations, so an open that fails after
/// wiring leaves the caller's registry and hooks as they were;
/// [`Self::keep`] keeps them for as long as the registry and hooks live.
#[derive(Default)]
pub(crate) struct Memory {
    registrations: Vec<Registration>,
}

impl Memory {
    /// Register the memory tools in `tools`, and the recall hook on
    /// `hooks.context_build` when `config` has `recall`. The store is the
    /// namespace's rows on `pool` (Postgres stores), else a fresh one in
    /// memory. Nothing when `config` is `None`.
    ///
    /// Raises:
    ///     OpenError: `Memory` for a `k` outside 1 to 20 or a `max_memories`
    ///         of 0; `Tools` when a tool name is taken.
    pub(crate) fn wire(
        config: Option<&MemoryConfig>,
        tools: &ToolRegistry,
        hooks: &Hooks,
        pool: Option<&PgPool>,
        namespace: &str,
    ) -> Result<Self, OpenError> {
        let Some(config) = config else {
            return Ok(Self::default());
        };
        if config.max_memories == Some(0) {
            return Err(OpenError::Memory("memory.max_memories must be at least 1"));
        }
        if (config.recall.as_ref())
            .is_some_and(|recall| !(1..=MAX_RECALL as u32).contains(&recall.k))
        {
            return Err(OpenError::Memory("memory.recall.k must be 1 to 20"));
        }
        let store: Arc<dyn MemoryStore> = match (pool, config.max_memories) {
            (Some(pool), None) => Arc::new(PgMemoryStore::new(pool.clone(), namespace)),
            (Some(pool), Some(max)) => {
                Arc::new(PgMemoryStore::new(pool.clone(), namespace).with_cap(max))
            }
            (None, None) => Arc::new(InMemoryMemoryStore::new()),
            (None, Some(max)) => Arc::new(InMemoryMemoryStore::new().with_cap(max)),
        };
        let mut wired = Self::default();
        for tool in memory_tools(Arc::clone(&store), config.scope) {
            wired.registrations.push(tools.register(tool)?);
        }
        if let Some(recall) = &config.recall {
            let hook = MemoryRecall::new(store, config.scope, recall.k as usize);
            let registration = (hooks.context_build.register(Arc::new(hook)))
                .expect("the recall hook has a policy id, no matcher and fails open");
            wired.registrations.push(registration);
        }
        Ok(wired)
    }

    /// Keep the tools and the hook registered.
    pub(crate) fn keep(mut self) {
        // Dropping a handle undoes nothing.
        self.registrations.clear();
    }
}

impl Drop for Memory {
    fn drop(&mut self) {
        for registration in &self.registrations {
            registration.dispose();
        }
    }
}
