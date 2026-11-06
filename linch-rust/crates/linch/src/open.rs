//! [`AgentConfig::open`]: config to provider, tools, policy, stores and
//! agent.

use std::path::PathBuf;
use std::sync::Arc;

use linch_core::{
    Agent, AgentBuilder, MemoryRunStore, MemorySessionStore, PermissionEngine, PermissionMode,
    PermissionPolicy, Provider, RunStore, ScriptedProvider, SessionStore, ToolRegistry,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use linch_providers::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ReasoningSummary, ResponsesOptions, Server,
    ServerOptions, anthropic, deepseek, gemini, openai,
};
use linch_tools::{LocalFs, ReadTracker, workspace_tools};

use crate::config::{AgentConfig, ProviderConfig, ReasoningSummaryConfig, StoreConfig};
use crate::error::OpenError;

impl AgentConfig {
    /// Open the agent this config describes.
    ///
    /// Reads the provider key from the environment, checks the workspace
    /// root, validates the permission policy, then opens the stores (for
    /// Postgres: connects a pool and runs the migrations) and builds the
    /// agent. Nothing is written to the stores.
    ///
    /// Returns:
    ///     The built agent, its contract digest computed over the spec, the
    ///     workspace tools and the (empty) hook policies.
    ///
    /// Raises:
    ///     OpenError: If the key is unset, the workspace is not a directory,
    ///         the policy is malformed, Postgres is unreachable, or the agent
    ///         cannot be built (a blank model or namespace).
    pub async fn open(self) -> Result<Agent, OpenError> {
        let provider = provider(self.provider)?;
        let tools = Arc::new(ToolRegistry::new());
        let root = match self.workspace {
            Some(root) => Some(register_workspace(&tools, root).await?),
            None => None,
        };
        let policy = self.permissions.unwrap_or_else(|| {
            let project_root = root.as_deref().unwrap_or("/");
            PermissionPolicy::new(PermissionMode::Default, project_root)
        });
        let permissions = PermissionEngine::new(policy)?;
        let (runs, sessions) = stores(self.stores).await?;
        let agent = AgentBuilder::new(self.spec)
            .provider(provider)
            .tools(tools)
            .permissions(permissions)
            .run_store(runs)
            .session_store(sessions)
            .namespace(self.namespace)
            .build()?;
        Ok(agent)
    }
}

/// Register the six workspace tools over `root`; returns the canonical
/// root as a string.
async fn register_workspace(tools: &ToolRegistry, root: PathBuf) -> Result<String, OpenError> {
    // `LocalFs::new` canonicalizes the root: blocking file system calls.
    let fs = match tokio::task::spawn_blocking(move || LocalFs::new(root)).await {
        Ok(fs) => fs.map_err(OpenError::Workspace)?,
        Err(join) => std::panic::resume_unwind(join.into_panic()),
    };
    let fs = Arc::new(fs);
    let canonical = fs.root().display().to_string();
    for tool in workspace_tools(fs, Arc::new(ReadTracker::new())) {
        // The registry lives as long as the agent; the handle is not needed.
        let _ = tools.register(tool)?;
    }
    Ok(canonical)
}

type Stores = (Arc<dyn RunStore>, Arc<dyn SessionStore>);

async fn stores(config: StoreConfig) -> Result<Stores, OpenError> {
    match config {
        StoreConfig::Memory {} => Ok((
            Arc::new(MemoryRunStore::default()),
            Arc::new(MemorySessionStore::default()),
        )),
        StoreConfig::Postgres { dsn } => {
            let pool = PgPool::connect(&dsn)
                .await
                .map_err(|error| OpenError::Store(Box::new(error)))?;
            linch_postgres::migrate(&pool)
                .await
                .map_err(|error| OpenError::Store(Box::new(error)))?;
            Ok((
                Arc::new(PgRunStore::new(pool.clone())),
                Arc::new(PgSessionStore::new(pool)),
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
