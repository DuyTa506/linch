//! [`AgentConfig::open`]: config to provider, tools, policy, stores, MCP
//! servers and agent, and the [`OpenedAgent`] that closes them.

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use linch_core::ask_user::AskUser;
use linch_core::{
    Agent, AgentBuilder, Hooks, InMemoryMailbox, Mailbox, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, Provider, RunStore, ScriptedProvider,
    SessionId, SessionStore, StoreError, TaskStore, Tool, ToolRegistry,
};
use linch_mcp::McpServer;
use linch_postgres::{PgMailbox, PgRunStore, PgSessionStore, PgToolCache};
use linch_providers::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    HttpClients, OpenAiChat, OpenAiChatOptions, OpenAiResponses, ReasoningSummary, RequestOptions,
    ResponsesOptions, Server, ServerOptions, anthropic, deepseek, gemini, openai,
};
use linch_tools::{
    DEFAULT_LEDGER_CHARS, ExecutionBackend, LocalFs, Processes, ReadTracker, RemoteExec,
    SCHEDULE_TOOL_NAMES, TASK_TOOL_NAMES, TaskLedgerContext, task_tools, task_tools_in,
    workspace_tools,
};

use crate::agent_memory::Memory;
use crate::config::{
    AgentConfig, ChatConfig, ExecutionConfig, McpConfig, ProviderConfig, ReasoningSummaryConfig,
    RequestConfig, StoreConfig, TaskLedger,
};
use crate::deep;
use crate::error::OpenError;
use crate::files::Files;
use crate::pools::{PgPools, Shared};
use crate::project::{self, Loaded, Skipped};
use linch_ext::deep_agent::DeepAgent;

mod reload;

impl AgentConfig {
    /// Open the agent this config describes, with no tools or hooks beyond
    /// what the config declares and a Postgres pool of its own. Same as
    /// [`Self::open_with`] on an empty registry, empty hooks and a fresh
    /// [`PgPools`].
    ///
    /// Raises:
    ///     OpenError: As for [`Self::open_with`].
    pub async fn open(self) -> Result<OpenedAgent, OpenError> {
        let tools = Arc::new(ToolRegistry::new());
        let hooks = Arc::new(Hooks::default());
        self.open_with(tools, hooks, &PgPools::new()).await
    }

    /// Open the agent this config describes on top of tools and hooks bound
    /// in code.
    ///
    /// First turns on what a `deep_agent` preset needs (see
    /// [`DeepAgentConfig`](crate::DeepAgentConfig); its file memory hook
    /// goes with the filesystem's registrations, and its roster joins the
    /// config directory's subagents), and applies the `profile` (its spec
    /// changes, and its permission layer on the policy). Then reads the
    /// provider key from the environment, runs the provider's
    /// [`Provider::prepare`] (a self-hosted server without a configured
    /// `context_window` is asked for one, for at most 2 s, and the open
    /// goes on whatever it answers), registers the workspace
    /// tools into `tools`, validates the permission policy, opens the stores
    /// (for Postgres: takes the pool for the DSN from `pools`, connecting it
    /// and running the migrations when no open agent uses one), registers
    /// the session task tools over the session store when `tasks` is on
    /// (kept on a failed open, as the workspace tools are), and `AskUser`
    /// when `ask_user` is on, registers the
    /// virtual filesystem tools and the offload hook when the config
    /// turns them on (see [`FilesystemConfig`](crate::FilesystemConfig); a
    /// failed open takes them back), connects
    /// the MCP servers in order, loads the skills and subagents of the
    /// config directory when they are on (no skill may take the name of a
    /// tool registered by then; see [`OpenedAgent::skipped`] for those
    /// left out), then builds the agent. Nothing is written to the stores. [`Agent::close_session`] drops what the workspace
    /// tools recorded as read in that session, so its next write must read
    /// again, and the session's in-memory virtual files, and kills (and
    /// reaps) its background `Bash` commands.
    ///
    /// Args:
    ///     tools: Registry the agent runs with. Tools already in it come
    ///         first; the workspace and MCP tools are added after them.
    ///     hooks: Hook chains the agent runs with.
    ///     pools: Postgres pools shared with the other agents opened with
    ///         it; unused with memory stores.
    ///
    /// Returns:
    ///     The built agent with the MCP servers it connected. Its contract
    ///     digest covers the spec (with the filesystem's system text), every
    ///     tool registered at that point and the hook policies. It prices
    ///     each paid call with [`linch_providers::cost`], so a model in that
    ///     table fills the cost fields of usage and result events.
    ///
    /// Raises:
    ///     OpenError: If the deep agent preset names an unknown profile or
    ///         is durable on memory stores, the domain profile is invalid,
    ///         the key is unset, the workspace
    ///         is not a directory,
    ///         a tool name is taken twice, the policy is malformed, Postgres
    ///         is unreachable, an MCP server cannot be connected, or the
    ///         agent cannot be built (a blank model or namespace). MCP
    ///         servers already connected are disconnected first.
    pub async fn open_with(
        self,
        tools: Arc<ToolRegistry>,
        hooks: Arc<Hooks>,
        pools: &PgPools,
    ) -> Result<OpenedAgent, OpenError> {
        self.open_with_remote(tools, hooks, pools, None).await
    }

    /// [`Self::open_with`] with the host that runs the commands of a
    /// `remote` execution backend, over a connection the caller keeps.
    ///
    /// Args:
    ///     remote: The host of the `remote` backend. Without it that
    ///         backend does not open, so a worker or a schedule, which have
    ///         no live connection, refuse such an agent. Unused by the
    ///         other backends. A `remote` backend whose policy does not
    ///         share the workspace registers only `Bash`, `BashOutput` and
    ///         `BashKill`: the file tools would act on the server's
    ///         directory, not where the commands run.
    ///
    /// Raises:
    ///     OpenError: As for [`Self::open_with`], and
    ///         [`OpenError::Execution`] when `execution` is `remote` and
    ///         `remote` is `None`.
    pub async fn open_with_remote(
        mut self,
        tools: Arc<ToolRegistry>,
        hooks: Arc<Hooks>,
        pools: &PgPools,
        remote: Option<Arc<dyn RemoteExec>>,
    ) -> Result<OpenedAgent, OpenError> {
        if self.schedule_tools {
            schedule_tools_bound(&self.stores, &tools)?;
        }
        let deep = deep::expand(&mut self)?;
        if let Some(profile) = &self.profile {
            profile.validate()?;
            self.spec = profile.spec(self.spec);
        }
        let gate = (self.max_provider_concurrency).map(|max| {
            pools.gate(
                crate::pools::GateKey::new(key_env(&self.provider), base_url(&self.provider)),
                max,
            )
        });
        let provider = provider(self.provider, true, Some(&pools.clients()))?;
        // A self-hosted server reports its context window here, before the
        // offload threshold and the skill listing are derived from it.
        provider.prepare().await;
        let window = provider.context_window(&self.spec.model);
        let reads = Arc::new(ReadTracker::new());
        let processes = Arc::new(Processes::new());
        let (root, backend) = match self.workspace {
            Some(root) => {
                let (reads, processes) = (Arc::clone(&reads), Arc::clone(&processes));
                let execution = self.bash.then(|| self.execution.unwrap_or_default());
                let (root, backend) =
                    register_workspace(&tools, root, reads, processes, execution, remote).await?;
                (Some(root), backend)
            }
            None => (None, None),
        };
        let mut policy = self.permissions.unwrap_or_else(|| {
            let project_root = root.as_deref().unwrap_or("/");
            PermissionPolicy::new(PermissionMode::Default, project_root)
        });
        if self.tasks {
            policy.auto_allow.extend(TASK_TOOL_NAMES.map(String::from));
        }
        policy
            .layers
            .extend(self.profile.and_then(|p| p.deny_layer()));
        let permissions = PermissionEngine::new(policy)?;
        let (runs, sessions, tasks, pool) = stores(self.stores, pools).await?;
        let ledger = match &self.task_ledger {
            None => None,
            Some(_) if !self.tasks => return Err(OpenError::TaskLedger("it needs tasks")),
            Some(ledger) if ledger.list.trim().is_empty() => {
                return Err(OpenError::TaskLedger("the list id is blank"));
            }
            Some(TaskLedger {
                max_chars: Some(max),
                ..
            }) if *max < 200 => {
                return Err(OpenError::TaskLedger("max_chars is under 200"));
            }
            Some(ledger) => Some(ledger.clone()),
        };
        let mut ledger_hook = None;
        if self.tasks {
            let list = ledger.as_ref().map(|l| SessionId::new(l.list.clone()));
            let made = match &list {
                Some(list) => {
                    // The list is a session of the store, created once.
                    match sessions.create(&self.namespace, list).await {
                        Ok(()) | Err(StoreError::Conflict { .. }) => {}
                        Err(error) => return Err(OpenError::Store(Box::new(error))),
                    }
                    let max = ledger.as_ref().and_then(|l| l.max_chars);
                    let max = max.unwrap_or(DEFAULT_LEDGER_CHARS);
                    let namespace = &self.namespace;
                    let hook =
                        TaskLedgerContext::new(Arc::clone(&tasks), namespace, list.clone(), max);
                    ledger_hook = Some(hook);
                    let runs = Arc::clone(&runs);
                    task_tools_in(tasks, runs, namespace, list.clone())
                }
                None => task_tools(tasks, Arc::clone(&runs), &self.namespace),
            };
            for tool in made {
                // The registry lives as long as the agent; the handle is not needed.
                let _ = tools.register(tool)?;
            }
        }
        if self.ask_user {
            // The registry lives as long as the agent; the handle is not needed.
            let _ = tools.register(Arc::new(AskUser::new()))?;
        }
        let pg = pool.as_deref().and_then(Shared::pool);
        let memory = Memory::wire(self.memory.as_ref(), &tools, &hooks, pg, &self.namespace)?;
        // Postgres stores keep the messages in a table too.
        let mailbox = self.mailbox.then(|| -> Arc<dyn Mailbox> {
            match pg {
                Some(pool) => Arc::new(PgMailbox::new(pool.clone(), &self.namespace)),
                None => Arc::new(InMemoryMailbox::new()),
            }
        });
        let mut files = Files::wire(
            self.filesystem.as_ref(),
            &tools,
            &hooks,
            pg,
            &self.namespace,
            window,
        )?;
        if deep.is_some() || self.filesystem.as_ref().is_some_and(|files| files.memory) {
            files.remember(
                &hooks,
                self.filesystem.as_ref().and_then(|f| f.memory_scope),
            );
        }
        if let Some(hook) = ledger_hook {
            files.also(
                hooks
                    .context_build
                    .register(Arc::new(hook))
                    .expect("the ledger hook has a policy id, no matcher and fails open"),
            );
        }
        let mcp = connect_mcp(self.mcp_servers, &tools).await?;
        let config_dir = project::dir(self.config_dir, root.as_deref());
        let mut project = match self.skills || self.subagents {
            true => project::load(config_dir.clone(), self.skills, self.subagents, &tools).await,
            false => Loaded::default(),
        };
        deep::add_roster(deep.as_ref(), &mut project);
        let registry = Arc::clone(&tools);
        let forget = files.forget();
        let unread = Arc::clone(&reads);
        let caches = self.spec.tool_cache.is_some();
        let mut builder = AgentBuilder::new(files.teach(self.spec))
            .provider(provider)
            .tools(tools)
            .hooks(hooks)
            .permissions(permissions)
            .run_store(runs)
            .session_store(sessions)
            .namespace(self.namespace)
            .pricing(crate::config::pricing(self.pricing))
            .on_session_close(move |session| {
                reads.forget(session);
                processes.close_session(session);
                if let Some(backend) = &backend {
                    backend.close_session(session);
                }
                forget(session);
            })
            // A compaction folds or clears what the model read: it reads a
            // file again before changing it (Python's `reset_read_tracker`).
            .on_compact(move |session| unread.forget(session));
        if let Some(gate) = gate {
            builder = builder.provider_gate(gate);
        }
        if let Some(skills) = project.skills {
            builder = builder.skills(skills);
        }
        if let Some(definitions) = project.subagents {
            builder = builder.subagents(definitions);
        }
        if let Some(workers) = self.workers {
            builder = builder.workers(workers);
        }
        if let Some(mailbox) = mailbox {
            builder = builder.mailbox(mailbox);
        }
        // Postgres stores keep cached tool results in a table shared by the
        // namespace's servers; memory stores leave the builder's own.
        if let (Some(pool), true) = (pg, caches) {
            builder = builder.tool_cache_store(Arc::new(PgToolCache::new(pool.clone())));
        }
        if let Some(search) = self.tool_search {
            builder = builder.tool_search(search);
        }
        let skipped = project.skipped;
        match builder.build() {
            Ok(agent) => {
                files.keep();
                memory.keep();
                Ok(OpenedAgent {
                    agent,
                    mcp,
                    pool,
                    skipped,
                    config_dir,
                    skills: self.skills,
                    subagents: self.subagents,
                    tools: registry,
                    deep,
                })
            }
            Err(error) => {
                disconnect(mcp).await;
                Err(error.into())
            }
        }
    }
}

/// An agent opened from an [`AgentConfig`], with the MCP servers it
/// connected.
///
/// Call [`Self::close`] when done. Dropping it instead closes the MCP
/// servers in the background, without waiting for their processes to exit.
pub struct OpenedAgent {
    agent: Agent,
    mcp: Vec<McpServer>,
    /// Keeps the Postgres pool in its [`PgPools`] while this agent is open.
    pool: Option<Arc<Shared>>,
    skipped: Vec<Skipped>,
    /// Where [`Self::reload_skills`] and [`Self::reload_subagents`] scan.
    config_dir: PathBuf,
    /// The config's `skills` and `subagents`: a reload reads the config
    /// directory only for a kind that was on, as the open did.
    skills: bool,
    subagents: bool,
    tools: Arc<ToolRegistry>,
    /// The deep agent preset, whose roster a reload keeps.
    deep: Option<DeepAgent>,
}

impl OpenedAgent {
    /// The agent. Clone it to run from several tasks.
    pub fn agent(&self) -> &Agent {
        &self.agent
    }

    /// The skill directories and subagent files of the config directory
    /// that were left out, and why.
    pub fn skipped(&self) -> &[Skipped] {
        &self.skipped
    }

    /// The connected MCP servers, in config order.
    pub fn mcp_servers(&self) -> &[McpServer] {
        &self.mcp
    }

    /// The Postgres pool the agent's stores use; `None` with memory
    /// stores. A server writes an agent's schedules and queued runs into
    /// this pool's database and schema.
    pub fn pool(&self) -> Option<&linch_postgres::PgPool> {
        self.pool.as_deref().and_then(Shared::pool)
    }

    /// Wait (at most [`RELEASE_WAIT`]) for the lease releases of runs
    /// that were dropped, so another process can resume them at once (see
    /// [`Agent::released`]); then disconnect every MCP server and wait for
    /// each to close (a stdio server's process to exit). Clones of the
    /// agent keep working, but the MCP tools are no longer registered; the
    /// workspace and virtual filesystem tools and the offload hook stay.
    /// The Postgres pool is not closed: other agents may share it, and it
    /// goes once nothing uses it.
    pub async fn close(self) {
        let _ = tokio::time::timeout(RELEASE_WAIT, self.agent.released()).await;
        drop(self.pool);
        disconnect(self.mcp).await;
    }
}

impl fmt::Debug for OpenedAgent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<&str> = self.mcp.iter().map(McpServer::name).collect();
        f.debug_struct("OpenedAgent")
            .field("contract", &self.agent.contract())
            .field("mcp_servers", &names)
            .finish_non_exhaustive()
    }
}

/// Connect each server in order, so the registry order (and the contract)
/// does not depend on timing. On a failure, disconnect the ones already up.
async fn connect_mcp(
    configs: Vec<McpConfig>,
    tools: &Arc<ToolRegistry>,
) -> Result<Vec<McpServer>, OpenError> {
    let mut servers = Vec::with_capacity(configs.len());
    for config in configs {
        match McpServer::connect(config.into(), Arc::clone(tools)).await {
            Ok(server) => servers.push(server),
            Err(error) => {
                disconnect(servers).await;
                return Err(OpenError::Mcp(error));
            }
        }
    }
    Ok(servers)
}

async fn disconnect(servers: Vec<McpServer>) {
    for server in servers {
        server.disconnect().await;
    }
}

/// Register the workspace tools over `root`, sharing `reads` and
/// `processes`, with `Bash`, `BashOutput` and `BashKill` running through
/// the `execution` backend only when it is given (`bash` is on); returns
/// the canonical root as a string, and the backend.
async fn register_workspace(
    tools: &ToolRegistry,
    root: PathBuf,
    reads: Arc<ReadTracker>,
    processes: Arc<Processes>,
    execution: Option<ExecutionConfig>,
    remote: Option<Arc<dyn RemoteExec>>,
) -> Result<(String, Option<ExecutionBackend>), OpenError> {
    let bash = execution.is_some();
    // `LocalFs::new` canonicalizes the root and a sandbox checks the
    // kernel enforces it: blocking calls.
    let opened = tokio::task::spawn_blocking(move || {
        let fs = Arc::new(LocalFs::new(root).map_err(OpenError::Workspace)?);
        let config = execution.unwrap_or_default();
        ExecutionBackend::open_with_host(fs, &config, remote).map_err(OpenError::Execution)
    });
    let backend = match opened.await {
        Ok(backend) => backend?,
        Err(join) => std::panic::resume_unwind(join.into_panic()),
    };
    let canonical = backend.root().display().to_string();
    let wanted = |tool: &Arc<dyn Tool>| bash || !BASH_TOOLS.contains(&tool.name());
    for tool in workspace_tools(&backend, reads, processes)
        .into_iter()
        .filter(wanted)
    {
        // The registry lives as long as the agent; the handle is not needed.
        let _ = tools.register(tool)?;
    }
    Ok((canonical, bash.then_some(backend)))
}

/// Refuse `schedule_tools` unless the stores are Postgres and the host
/// already registered the schedule tools: the facade cannot run the
/// schedules they make.
fn schedule_tools_bound(stores: &StoreConfig, tools: &ToolRegistry) -> Result<(), OpenError> {
    if !matches!(stores, StoreConfig::Postgres { .. }) {
        return Err(OpenError::ScheduleTools("schedules need Postgres stores"));
    }
    let snapshot = tools.snapshot();
    if !SCHEDULE_TOOL_NAMES
        .iter()
        .all(|name| snapshot.resolve(name).is_some())
    {
        return Err(OpenError::ScheduleTools(
            "the schedule tools are bound by a host that runs schedules: open the agent \
             through linch-server, for a tenant whose allowlists give it schedules",
        ));
    }
    Ok(())
}

/// The longest [`OpenedAgent::close`] waits for the lease releases of
/// dropped runs: each is one small write, so a database that does not take
/// it within this long leaves those runs to wait out their leases.
pub const RELEASE_WAIT: Duration = Duration::from_secs(5);

/// The workspace tools that run commands, which the `bash` switch turns off
/// together.
const BASH_TOOLS: [&str; 3] = ["Bash", "BashOutput", "BashKill"];

/// The run and session stores, the session store again as the task
/// store, and for Postgres the pool's slot in `pools`, which the opened
/// agent holds.
type Stores = (
    Arc<dyn RunStore>,
    Arc<dyn SessionStore>,
    Arc<dyn TaskStore>,
    Option<Arc<Shared>>,
);

async fn stores(config: StoreConfig, pools: &PgPools) -> Result<Stores, OpenError> {
    match config {
        StoreConfig::Memory {} => {
            let sessions = Arc::new(MemorySessionStore::default());
            let runs = Arc::new(MemoryRunStore::default());
            Ok((runs, sessions.clone(), sessions, None))
        }
        StoreConfig::Postgres {
            dsn,
            max_connections,
            lease_ttl_ms,
        } => {
            let (shared, pool) = pools.get(&dsn, max_connections).await?;
            let sessions = Arc::new(PgSessionStore::new(pool.clone()));
            let runs = PgRunStore::new(pool);
            let runs = Arc::new(match lease_ttl_ms {
                Some(ms) => runs.with_lease_ttl(Duration::from_millis(ms)),
                None => runs,
            });
            Ok((runs, sessions.clone(), sessions, Some(shared)))
        }
    }
}

impl ProviderConfig {
    /// Build the provider this config names, as [`AgentConfig::open`]
    /// does: its key is read from the process environment, and a missing
    /// one is [`OpenError::MissingKey`]. It does not call
    /// [`Provider::prepare`].
    ///
    /// Raises:
    ///     OpenError: The key is missing or the HTTP client cannot be built.
    pub fn build(&self) -> Result<Arc<dyn Provider>, OpenError> {
        provider(self.clone(), true, None)
    }

    /// Like [`Self::build`], but the environment is never read for a key:
    /// the provider holds a placeholder that no request may carry (a
    /// self-hosted server's optional key is left unset), so a call that
    /// would send the key sends nothing. For the questions a provider
    /// answers without one, [`Provider::capabilities`], and
    /// [`Provider::prepare`], which then probes a self-hosted server
    /// unauthenticated.
    ///
    /// Raises:
    ///     OpenError: The HTTP client cannot be built.
    pub fn build_keyless(&self) -> Result<Arc<dyn Provider>, OpenError> {
        provider(self.clone(), false, None)
    }
}

/// The request options of `request`, sharing the HTTP clients of `clients`
/// when the provider is opened by an agent (see [`PgPools`]).
fn request_options(
    request: &RequestConfig,
    clients: Option<&HttpClients>,
) -> Result<RequestOptions, OpenError> {
    let options = request.options().map_err(OpenError::Provider)?;
    Ok(match clients {
        Some(clients) => options.shared_clients(clients.clone()),
        None => options,
    })
}

fn provider(
    config: ProviderConfig,
    keyed: bool,
    clients: Option<&HttpClients>,
) -> Result<Arc<dyn Provider>, OpenError> {
    let built: Arc<dyn Provider> = match config {
        ProviderConfig::Scripted {
            turns,
            context_window,
            native_output_schema,
        } => {
            let mut scripted = ScriptedProvider::new(turns);
            if !native_output_schema {
                scripted = scripted.without_native_output_schema();
            }
            return Ok(Arc::new(match context_window {
                Some(window) => scripted.with_context_window(window),
                None => scripted,
            }));
        }
        ProviderConfig::DeepSeek {
            base_url,
            context_window,
            request,
            chat,
        } => {
            let mut options =
                DeepSeekOptions::new(key("deepseek", deepseek::API_KEY_ENV, keyed, &request)?);
            override_url(&mut options.base_url, base_url);
            options.context_window = context_window;
            options.request = request_options(&request, clients)?;
            options.chat = chat.options();
            Arc::new(DeepSeek::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::OpenAiChat {
            base_url,
            context_window,
            request,
            chat,
        } => {
            let mut options =
                OpenAiChatOptions::new(key("openai-chat", openai::API_KEY_ENV, keyed, &request)?);
            override_url(&mut options.base_url, base_url);
            options.context_window = context_window;
            options.request = request_options(&request, clients)?;
            options.chat = chat.options();
            Arc::new(OpenAiChat::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::OpenAiResponses {
            base_url,
            reasoning_summary,
            store,
            native_catalog_changes,
            request,
        } => {
            let api_key = key("openai-responses", openai::API_KEY_ENV, keyed, &request)?;
            let mut options = ResponsesOptions::new(api_key);
            override_url(&mut options.base_url, base_url);
            options.reasoning_summary = reasoning_summary.map(summary);
            options.store = store;
            options.native_catalog_changes = native_catalog_changes;
            options.request = request_options(&request, clients)?;
            Arc::new(OpenAiResponses::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Anthropic {
            base_url,
            native_catalog_changes,
            request,
        } => {
            let from_env = AnthropicOptions::from_env().filter(|_| keyed);
            let mut options = match from_env {
                Some(options) => options,
                None => AnthropicOptions::new(key(
                    "anthropic",
                    anthropic::API_KEY_ENV,
                    keyed,
                    &request,
                )?),
            };
            override_url(&mut options.base_url, base_url);
            options.native_catalog_changes = native_catalog_changes;
            options.request = request_options(&request, clients)?;
            Arc::new(Anthropic::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Gemini { base_url, request } => {
            let mut options =
                GeminiOptions::new(key("gemini", gemini::API_KEY_ENV, keyed, &request)?);
            override_url(&mut options.base_url, base_url);
            options.request = request_options(&request, clients)?;
            Arc::new(Gemini::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Vllm {
            base_url,
            context_window,
            request,
            chat,
        } => server(
            Server::Vllm,
            base_url,
            context_window,
            request_options(&request, clients)?,
            &chat,
            "VLLM_API_KEY",
            keyed,
        )?,
        ProviderConfig::SgLang {
            base_url,
            context_window,
            request,
            chat,
        } => server(
            Server::SgLang,
            base_url,
            context_window,
            request_options(&request, clients)?,
            &chat,
            "SGLANG_API_KEY",
            keyed,
        )?,
        ProviderConfig::LlamaCpp {
            base_url,
            context_window,
            request,
            chat,
        } => server(
            Server::LlamaCpp,
            base_url,
            context_window,
            request_options(&request, clients)?,
            &chat,
            "LLAMA_API_KEY",
            keyed,
        )?,
    };
    Ok(built)
}

/// The variable `config`'s provider reads its key from, which names the
/// key a provider gate is shared by; empty for the scripted provider, which
/// has none.
fn key_env(config: &ProviderConfig) -> &'static str {
    match config {
        ProviderConfig::DeepSeek { .. } => deepseek::API_KEY_ENV,
        ProviderConfig::OpenAiChat { .. } | ProviderConfig::OpenAiResponses { .. } => {
            openai::API_KEY_ENV
        }
        ProviderConfig::Anthropic { .. } => anthropic::API_KEY_ENV,
        ProviderConfig::Gemini { .. } => gemini::API_KEY_ENV,
        ProviderConfig::Vllm { .. } => "VLLM_API_KEY",
        ProviderConfig::SgLang { .. } => "SGLANG_API_KEY",
        ProviderConfig::LlamaCpp { .. } => "LLAMA_API_KEY",
        ProviderConfig::Scripted { .. } => "",
    }
}

/// The endpoint `config` names; `None` for the provider's own.
fn base_url(config: &ProviderConfig) -> Option<&str> {
    match config {
        ProviderConfig::DeepSeek { base_url, .. }
        | ProviderConfig::OpenAiChat { base_url, .. }
        | ProviderConfig::OpenAiResponses { base_url, .. }
        | ProviderConfig::Anthropic { base_url, .. }
        | ProviderConfig::Gemini { base_url, .. } => base_url.as_deref(),
        ProviderConfig::Vllm { base_url, .. }
        | ProviderConfig::SgLang { base_url, .. }
        | ProviderConfig::LlamaCpp { base_url, .. } => Some(base_url),
        ProviderConfig::Scripted { .. } => None,
    }
}

/// The key in `env`, or [`OpenError::MissingKey`]; when `keyed` is false
/// the environment is not read and the key is a placeholder that is never
/// sent.
///
/// A request that sets its own key header (`authorization`, `x-api-key`,
/// `x-goog-api-key`) needs no key in the environment: the adapter does not
/// send this stand-in.
fn key(
    provider: &'static str,
    env: &'static str,
    keyed: bool,
    request: &RequestConfig,
) -> Result<ApiKey, OpenError> {
    if !keyed {
        return Ok(ApiKey::new("unset").expect("a non-empty placeholder"));
    }
    match ApiKey::from_env(env) {
        Some(key) => Ok(key),
        None if request.sets_key_header() => {
            Ok(ApiKey::new("set-by-request-header").expect("not blank"))
        }
        None => Err(OpenError::MissingKey { provider, env }),
    }
}

fn override_url(target: &mut String, base_url: Option<String>) {
    if let Some(base_url) = base_url {
        *target = base_url;
    }
}

/// A self-hosted server; its key is optional, and without a
/// `context_window` it is asked for one when the agent opens.
fn server(
    server: Server,
    base_url: String,
    context_window: Option<u64>,
    request: RequestOptions,
    chat: &ChatConfig,
    env: &'static str,
    keyed: bool,
) -> Result<Arc<dyn Provider>, OpenError> {
    let mut options = ServerOptions::new(server, base_url);
    options.api_key = ApiKey::from_env(env).filter(|_| keyed);
    options.context_window = context_window;
    options.request = request;
    options.chat = chat.options();
    Ok(Arc::new(
        OpenAiChat::server(options).map_err(OpenError::Provider)?,
    ))
}

fn summary(config: ReasoningSummaryConfig) -> ReasoningSummary {
    match config {
        ReasoningSummaryConfig::Auto => ReasoningSummary::Auto,
        ReasoningSummaryConfig::Concise => ReasoningSummary::Concise,
        ReasoningSummaryConfig::Detailed => ReasoningSummary::Detailed,
    }
}
