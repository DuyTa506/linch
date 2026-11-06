//! [`AgentConfig::open`]: config to provider, tools, policy, stores, MCP
//! servers and agent, and the [`OpenedAgent`] that closes them.

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;

use linch_core::{
    Agent, AgentBuilder, Hooks, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, Provider, RunStore, ScriptedProvider, SessionStore,
    ToolRegistry,
};
use linch_mcp::McpServer;
use linch_postgres::{PgRunStore, PgSessionStore};
use linch_providers::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ReasoningSummary, ResponsesOptions, Server,
    ServerOptions, anthropic, deepseek, gemini, openai,
};
use linch_tools::{LocalFs, ReadTracker, workspace_tools};

use crate::config::{AgentConfig, McpConfig, ProviderConfig, ReasoningSummaryConfig, StoreConfig};
use crate::error::OpenError;
use crate::pools::{PgPools, Shared};

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
    /// Reads the provider key from the environment, registers the workspace
    /// tools into `tools`, validates the permission policy, opens the stores
    /// (for Postgres: takes the pool for the DSN from `pools`, connecting it
    /// and running the migrations when no open agent uses one), connects
    /// the MCP servers in order, then builds the agent. Nothing is written
    /// to the stores. [`Agent::close_session`] drops what the workspace
    /// tools recorded as read in that session, so its next write must read
    /// again.
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
    ///     digest covers the spec, every tool registered at that point and
    ///     the hook policies.
    ///
    /// Raises:
    ///     OpenError: If the key is unset, the workspace is not a directory,
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
        let provider = provider(self.provider)?;
        let reads = Arc::new(ReadTracker::new());
        let root = match self.workspace {
            Some(root) => Some(register_workspace(&tools, root, Arc::clone(&reads)).await?),
            None => None,
        };
        let policy = self.permissions.unwrap_or_else(|| {
            let project_root = root.as_deref().unwrap_or("/");
            PermissionPolicy::new(PermissionMode::Default, project_root)
        });
        let permissions = PermissionEngine::new(policy)?;
        let (runs, sessions, pool) = stores(self.stores, pools).await?;
        let mcp = connect_mcp(self.mcp_servers, &tools).await?;
        let built = AgentBuilder::new(self.spec)
            .provider(provider)
            .tools(tools)
            .hooks(hooks)
            .permissions(permissions)
            .run_store(runs)
            .session_store(sessions)
            .namespace(self.namespace)
            .on_session_close(move |session| reads.forget(session))
            .build();
        match built {
            Ok(agent) => Ok(OpenedAgent { agent, mcp, pool }),
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
}

impl OpenedAgent {
    /// The agent. Clone it to run from several tasks.
    pub fn agent(&self) -> &Agent {
        &self.agent
    }

    /// The connected MCP servers, in config order.
    pub fn mcp_servers(&self) -> &[McpServer] {
        &self.mcp
    }

    /// Disconnect every MCP server and wait for each to close (a stdio
    /// server's process to exit). Clones of the agent keep working, but
    /// the MCP tools are no longer registered. The Postgres pool is not
    /// closed: other agents may share it, and it goes once nothing uses it.
    pub async fn close(self) {
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

/// Register the six workspace tools over `root`, sharing `reads`; returns
/// the canonical root as a string.
async fn register_workspace(
    tools: &ToolRegistry,
    root: PathBuf,
    reads: Arc<ReadTracker>,
) -> Result<String, OpenError> {
    // `LocalFs::new` canonicalizes the root: blocking file system calls.
    let fs = match tokio::task::spawn_blocking(move || LocalFs::new(root)).await {
        Ok(fs) => fs.map_err(OpenError::Workspace)?,
        Err(join) => std::panic::resume_unwind(join.into_panic()),
    };
    let fs = Arc::new(fs);
    let canonical = fs.root().display().to_string();
    for tool in workspace_tools(fs, reads) {
        // The registry lives as long as the agent; the handle is not needed.
        let _ = tools.register(tool)?;
    }
    Ok(canonical)
}

/// The run and session stores, and for Postgres the pool's slot in
/// `pools`, which the opened agent holds.
type Stores = (
    Arc<dyn RunStore>,
    Arc<dyn SessionStore>,
    Option<Arc<Shared>>,
);

async fn stores(config: StoreConfig, pools: &PgPools) -> Result<Stores, OpenError> {
    match config {
        StoreConfig::Memory {} => Ok((
            Arc::new(MemoryRunStore::default()),
            Arc::new(MemorySessionStore::default()),
            None,
        )),
        StoreConfig::Postgres { dsn } => {
            let (shared, pool) = pools.get(&dsn).await?;
            Ok((
                Arc::new(PgRunStore::new(pool.clone())),
                Arc::new(PgSessionStore::new(pool)),
                Some(shared),
            ))
        }
    }
}

fn provider(config: ProviderConfig) -> Result<Arc<dyn Provider>, OpenError> {
    let built: Arc<dyn Provider> = match config {
        ProviderConfig::Scripted { turns } => return Ok(Arc::new(ScriptedProvider::new(turns))),
        ProviderConfig::DeepSeek { base_url } => {
            let mut options = DeepSeekOptions::new(key("deepseek", deepseek::API_KEY_ENV)?);
            override_url(&mut options.base_url, base_url);
            Arc::new(DeepSeek::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::OpenAiChat { base_url } => {
            let mut options = OpenAiChatOptions::new(key("openai-chat", openai::API_KEY_ENV)?);
            override_url(&mut options.base_url, base_url);
            Arc::new(OpenAiChat::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::OpenAiResponses {
            base_url,
            reasoning_summary,
            store,
        } => {
            let api_key = key("openai-responses", openai::API_KEY_ENV)?;
            let mut options = ResponsesOptions::new(api_key);
            override_url(&mut options.base_url, base_url);
            options.reasoning_summary = reasoning_summary.map(summary);
            options.store = store;
            Arc::new(OpenAiResponses::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Anthropic { base_url } => {
            let mut options = AnthropicOptions::from_env().ok_or(OpenError::MissingKey {
                provider: "anthropic",
                env: anthropic::API_KEY_ENV,
            })?;
            override_url(&mut options.base_url, base_url);
            Arc::new(Anthropic::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Gemini { base_url } => {
            let mut options = GeminiOptions::new(key("gemini", gemini::API_KEY_ENV)?);
            override_url(&mut options.base_url, base_url);
            Arc::new(Gemini::new(options).map_err(OpenError::Provider)?)
        }
        ProviderConfig::Vllm { base_url } => server(Server::Vllm, base_url, "VLLM_API_KEY")?,
        ProviderConfig::SgLang { base_url } => server(Server::SgLang, base_url, "SGLANG_API_KEY")?,
        ProviderConfig::LlamaCpp { base_url } => {
            server(Server::LlamaCpp, base_url, "LLAMA_API_KEY")?
        }
    };
    Ok(built)
}

/// The key in `env`, or [`OpenError::MissingKey`].
fn key(provider: &'static str, env: &'static str) -> Result<ApiKey, OpenError> {
    ApiKey::from_env(env).ok_or(OpenError::MissingKey { provider, env })
}

fn override_url(target: &mut String, base_url: Option<String>) {
    if let Some(base_url) = base_url {
        *target = base_url;
    }
}

/// A self-hosted server; its key is optional.
fn server(
    server: Server,
    base_url: String,
    env: &'static str,
) -> Result<Arc<dyn Provider>, OpenError> {
    let mut options = ServerOptions::new(server, base_url);
    options.api_key = ApiKey::from_env(env);
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
