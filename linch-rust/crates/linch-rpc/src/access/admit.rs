//! The checks a tenant's requests pass before anything runs: no key is
//! read, no MCP process is started and no database is connected for a
//! refused request.

use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use linch::permissions::RuleDecision;
use linch::{
    AgentConfig, McpTransportConfig, PermissionMode, PermissionPolicy, Principal, ProviderConfig,
    StoreConfig,
};
use serde_json::json;

use super::allowlists::{McpCommand, PgTarget, allows};
use super::{Allowlists, Tenant};
use crate::error::{ErrorObject, code};

impl Tenant {
    /// Refuse `config` unless its namespace is this tenant's and its
    /// provider, endpoints, permissions, workspace, store and MCP servers
    /// are all allowed. The workspace, the project root and the config
    /// directory are replaced by the paths that were checked, and `bash` is
    /// turned off unless the tenant may run exec tools.
    pub(crate) async fn admit(&self, config: &mut AgentConfig) -> Result<(), ErrorObject> {
        let namespace = &config.namespace;
        let own = namespace == &self.name
            || (namespace.strip_prefix(self.name.as_str()))
                .is_some_and(|rest| rest.starts_with('/'));
        if !own {
            let message = format!(
                "namespace must be {:?} or start with \"{}/\"",
                self.name, self.name
            );
            return Err(refused("namespace", message));
        }
        let allow = &self.allow;
        allow.admit_provider(&config.provider)?;
        let mode = (config.permissions.as_ref()).map_or(PermissionMode::default(), |p| p.mode);
        if !allow.permission_modes.contains(&mode) {
            let message =
                format!("permission mode {mode:?} is not in the tenant's permission_modes");
            return Err(refused("permission_mode", message));
        }
        let unasked = config.tasks || config.deep_agent.is_some();
        if unasked
            && !allow
                .permission_modes
                .contains(&PermissionMode::SkipDangerous)
        {
            // The facade adds the task tools to `auto_allow` after these checks.
            let message = "tasks and deep_agent auto-allow the task tools, so they need \
                           skip_dangerous in the tenant's permission_modes";
            return Err(refused("auto_allow", message.into()));
        }
        if let Some(policy) = &mut config.permissions {
            allow.admit_grants(policy)?;
            let root = PathBuf::from(&policy.project_root);
            let canonical = allow.admit_dir(root, "project_root").await?;
            let Ok(canonical) = canonical.into_os_string().into_string() else {
                let message = "project_root resolves to a path that is not UTF-8";
                return Err(refused("project_root", message.into()));
            };
            policy.project_root = canonical;
        }
        if let Some(root) = config.workspace.take() {
            config.workspace = Some(allow.admit_dir(root, "workspace").await?);
        }
        if config.skills || config.subagents {
            let workspace = config.workspace.as_deref();
            let dir = allow.admit_config_dir(config.config_dir.take(), workspace);
            config.config_dir = Some(dir.await?);
        }
        config.bash &= allow.exec_tools;
        if let StoreConfig::Postgres { dsn } = &config.stores
            && !PgTarget::parse(dsn).is_some_and(|target| allow.postgres.contains(&target))
        {
            let message = "the store DSN does not name a server in the tenant's postgres_dsns";
            return Err(refused("postgres", message.into()));
        }
        for server in &config.mcp_servers {
            let refusal = match &server.transport {
                McpTransportConfig::Stdio { command, args, env } => {
                    let entries: Vec<_> = (allow.mcp_commands.iter())
                        .filter(|entry| entry.command == *command && entry.args == *args)
                        .collect();
                    let env_allowed =
                        |entry: &&McpCommand| env.keys().all(|name| entry.env.contains(name));
                    if entries.is_empty() {
                        Some("mcp_command")
                    } else if !entries.iter().any(env_allowed) {
                        Some("mcp_env")
                    } else {
                        None
                    }
                }
                McpTransportConfig::Http { url, .. } => {
                    (!allows(&allow.mcp_origins, url)).then_some("mcp_url")
                }
            };
            if let Some(kind) = refusal {
                let message = format!(
                    "MCP server {:?}: its transport is not in the tenant's allowlists ({kind})",
                    server.name
                );
                return Err(refused(kind, message));
            }
        }
        Ok(())
    }

    /// Refuse a principal that names another tenant; none is fine.
    pub(crate) fn admit_principal(&self, principal: Option<&Principal>) -> Result<(), ErrorObject> {
        match principal {
            Some(principal) if principal.tenant() != self.name => Err(refused(
                "principal",
                format!(
                    "principal.tenant must be the connection's tenant {:?}",
                    self.name
                ),
            )),
            _ => Ok(()),
        }
    }
}

impl Allowlists {
    fn admit_provider(&self, provider: &ProviderConfig) -> Result<(), ErrorObject> {
        if matches!(provider, ProviderConfig::Scripted { .. }) && !self.scripted_provider {
            let message = "the scripted provider is not allowed on this connection";
            return Err(refused("provider", message.into()));
        }
        match base_url(provider) {
            Some(url) if !allows(&self.provider_origins, url) => {
                let message = "provider base_url is not in the tenant's provider_base_urls, \
                               so the server's key may not be sent there";
                Err(refused("provider_base_url", message.into()))
            }
            _ => Ok(()),
        }
    }

    /// The canonical `path` (the config's `kind`), if it lies in an
    /// allowed root. A path that does not exist is refused the same way, so
    /// a client cannot probe the server's file system.
    async fn admit_dir(&self, path: PathBuf, kind: &str) -> Result<PathBuf, ErrorObject> {
        if let Ok(canonical) = tokio::fs::canonicalize(&path).await
            && (self.workspace_roots.iter()).any(|allowed| canonical.starts_with(allowed))
        {
            return Ok(canonical);
        }
        let message =
            format!("{kind} is not an existing directory under the tenant's workspace_roots");
        Err(refused(kind, message))
    }

    /// The directory skills and subagents load from, resolved as the
    /// facade does: a relative one against the workspace, else the server's
    /// working directory. One the config names must pass
    /// [`Self::admit_dir`]. The default `.linch` may be missing, as nothing
    /// then loads, but when anything is there, a symlink included, it must
    /// pass too; the client did not choose that path, so this probes
    /// nothing.
    async fn admit_config_dir(
        &self,
        dir: Option<PathBuf>,
        workspace: Option<&Path>,
    ) -> Result<PathBuf, ErrorObject> {
        let resolve = |dir: &Path| match workspace {
            Some(root) => root.join(dir),
            None => std::path::absolute(dir).unwrap_or_else(|_| dir.to_path_buf()),
        };
        match dir {
            Some(dir) => self.admit_dir(resolve(&dir), "config_dir").await,
            None => {
                let default = resolve(Path::new(".linch"));
                match tokio::fs::symlink_metadata(&default).await {
                    Err(error) if error.kind() == ErrorKind::NotFound => Ok(default),
                    _ => self.admit_dir(default, "config_dir").await,
                }
            }
        }
    }

    /// Refuse what lets calls run without asking, `auto_allow` entries and
    /// rules that allow, unless the tenant may choose `skip_dangerous`,
    /// which allows every call. Rules that ask or deny only take away.
    fn admit_grants(&self, policy: &PermissionPolicy) -> Result<(), ErrorObject> {
        if self
            .permission_modes
            .contains(&PermissionMode::SkipDangerous)
        {
            return Ok(());
        }
        if !policy.auto_allow.is_empty() {
            let message = "auto_allow needs skip_dangerous in the tenant's permission_modes";
            return Err(refused("auto_allow", message.into()));
        }
        let rules = policy.layers.iter().flat_map(|layer| &layer.rules);
        if rules
            .into_iter()
            .any(|rule| rule.decision() == RuleDecision::Allow)
        {
            let message =
                "a rule that allows needs skip_dangerous in the tenant's permission_modes";
            return Err(refused("permission_rule", message.into()));
        }
        Ok(())
    }
}

/// The endpoint a provider would send the server's key to, when the
/// client chose it. Absent: the provider's public endpoint, or the
/// server's own `ANTHROPIC_BASE_URL`.
fn base_url(provider: &ProviderConfig) -> Option<&str> {
    match provider {
        ProviderConfig::DeepSeek { base_url }
        | ProviderConfig::OpenAiChat { base_url }
        | ProviderConfig::OpenAiResponses { base_url, .. }
        | ProviderConfig::Anthropic { base_url }
        | ProviderConfig::Gemini { base_url } => base_url.as_deref(),
        // Their keys are optional, but a key set in the environment is
        // still sent.
        ProviderConfig::Vllm { base_url }
        | ProviderConfig::SgLang { base_url }
        | ProviderConfig::LlamaCpp { base_url } => Some(base_url),
        ProviderConfig::Scripted { .. } => None,
    }
}

fn refused(kind: &str, message: String) -> ErrorObject {
    ErrorObject::new(code::NOT_ALLOWED, message).with_data(json!({ "kind": kind }))
}
