//! The virtual filesystem a [`FilesystemConfig`] turns on: one per-session
//! file lookup shared by the four tools and the offload hook, so the model
//! can read back what offload wrote.

use std::sync::Arc;

use linch_core::{AgentSpec, Hooks, Registration, SessionId, ToolRegistry};
use linch_ext::deep_agent::FileMemoryContext;
use linch_postgres::{PgFileBackend, PgPool};
use linch_tools::vfs::{
    MemorySessions, OffloadConfig, PROMPT, ResultOffload, SessionFiles, SharedFiles, vfs_tools,
};

use crate::config::FilesystemConfig;
use crate::error::OpenError;

/// What [`Files::wire`] added to the registry and the hooks.
///
/// Dropping it undoes those registrations, so an open that fails after
/// wiring leaves the caller's registry and hooks as they were; [`Self::keep`]
/// keeps them for as long as the registry and hooks live.
#[derive(Default)]
pub(crate) struct Files {
    on: bool,
    registrations: Vec<Registration>,
    /// The per-session memory files, which a session close drops.
    memory: Option<Arc<MemorySessions>>,
    /// Each session's files; `None` when the filesystem is off.
    files: Option<Arc<dyn SessionFiles>>,
}

impl Files {
    /// Register the four tools in `tools`, and the offload hook on
    /// `hooks.post_tool_use` when `config` sets a threshold. The files are
    /// the namespace's `linch_files` rows on `pool` (Postgres stores), else
    /// a fresh memory backend per session. Nothing when `config` is `None`.
    ///
    /// Raises:
    ///     OpenError: `Tools` when one of the four names is taken.
    pub(crate) fn wire(
        config: Option<&FilesystemConfig>,
        tools: &ToolRegistry,
        hooks: &Hooks,
        pool: Option<&PgPool>,
        namespace: &str,
    ) -> Result<Self, OpenError> {
        let Some(config) = config else {
            return Ok(Self::default());
        };
        let mut wired = Self {
            on: true,
            registrations: Vec::new(),
            memory: None,
            files: None,
        };
        let files: Arc<dyn SessionFiles> = match pool {
            Some(pool) => {
                let backend = PgFileBackend::new(pool.clone(), namespace);
                Arc::new(SharedFiles::new(Arc::new(backend)))
            }
            None => {
                let memory = Arc::new(MemorySessions::new());
                wired.memory = Some(Arc::clone(&memory));
                memory
            }
        };
        wired.files = Some(Arc::clone(&files));
        for tool in vfs_tools(Arc::clone(&files)) {
            wired.registrations.push(tools.register(tool)?);
        }
        if let Some(threshold) = config.offload_threshold_tokens {
            let offload = ResultOffload::new(files, OffloadConfig::new(threshold))
                .expect("the default offload prefix is a valid path");
            let registration = (hooks.post_tool_use.register(Arc::new(offload)))
                .expect("the offload hook has a policy id, no matcher and fails open");
            wired.registrations.push(registration);
        }
        Ok(wired)
    }

    /// Register the deep agent's file memory hook over these files on
    /// `hooks.context_build`, undone like the other registrations. Nothing
    /// when the filesystem is off.
    pub(crate) fn remember(&mut self, hooks: &Hooks) {
        let Some(files) = self.files.clone() else {
            return;
        };
        let hook = FileMemoryContext::new(move |session| files.backend(session));
        let registration = (hooks.context_build.register(Arc::new(hook)))
            .expect("the file memory hook has a policy id, no matcher and fails open");
        self.registrations.push(registration);
    }

    /// `spec` with [`PROMPT`] after its system prompt when the tools are on.
    pub(crate) fn teach(&self, mut spec: AgentSpec) -> AgentSpec {
        if self.on {
            spec.system_prompt = match spec.system_prompt.is_empty() {
                true => PROMPT.to_owned(),
                false => format!("{}\n\n{PROMPT}", spec.system_prompt),
            };
        }
        spec
    }

    /// The session-close callback: drops the session's memory files.
    pub(crate) fn forget(&self) -> impl Fn(&SessionId) + Send + Sync + 'static {
        let memory = self.memory.clone();
        move |session| {
            if let Some(memory) = &memory {
                memory.forget(session);
            }
        }
    }

    /// Keep the tools and the hook registered.
    pub(crate) fn keep(mut self) {
        // Dropping a handle undoes nothing.
        self.registrations.clear();
    }
}

impl Drop for Files {
    fn drop(&mut self) {
        for registration in &self.registrations {
            registration.dispose();
        }
    }
}
