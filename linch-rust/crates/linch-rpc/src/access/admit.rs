//! The checks a tenant's requests pass before anything runs: no key is
//! read, no MCP process is started and no database is connected for a
//! refused request.

use std::path::PathBuf;

use linch::{
    AgentConfig, McpTransportConfig, PermissionMode, Principal, ProviderConfig, StoreConfig,
};
use serde_json::json;

use super::allowlists::{PgTarget, allows};
use super::{Allowlists, Tenant};
use crate::error::{ErrorObject, code};

impl Tenant {
    /// Refuse `config` unless its namespace is this tenant's and its
    /// provider, endpoints, permission mode, workspace, store and MCP
    /// servers are all allowed. The workspace is replaced by the
    /// canonical path that was checked.
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
        if let Some(root) = config.workspace.take() {
            config.workspace = Some(allow.admit_workspace(root).await?);
        }
        if let StoreConfig::Postgres { dsn } = &config.stores
            && !PgTarget::parse(dsn).is_some_and(|target| allow.postgres.contains(&target))
        {
            let message = "the store DSN does not name a server in the tenant's postgres_dsns";
            return Err(refused("postgres", message.into()));
        }
        for server in &config.mcp_servers {
            let (kind, allowed) = match &server.transport {
                McpTransportConfig::Stdio { command, .. } => {
                    ("mcp_command", allow.mcp_commands.contains(command))
                }
                McpTransportConfig::Http { url, .. } => {
                    ("mcp_url", allows(&allow.mcp_origins, url))
                }
            };
            if !allowed {
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

    /// The canonical `root`, if it lies in an allowed root. A path that
    /// does not exist is refused the same way, so a client cannot probe the
    /// server's file system.
    async fn admit_workspace(&self, root: PathBuf) -> Result<PathBuf, ErrorObject> {
        if let Ok(canonical) = tokio::fs::canonicalize(&root).await
            && (self.workspace_roots.iter()).any(|allowed| canonical.starts_with(allowed))
        {
            return Ok(canonical);
        }
        let message = "workspace is not an existing directory under the tenant's workspace_roots";
        Err(refused("workspace", message.into()))
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
