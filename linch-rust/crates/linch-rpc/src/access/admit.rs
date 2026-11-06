//! The checks a tenant's requests pass before anything runs: no key is
//! read, no MCP process is started and no database is connected for a
//! refused request.

use std::path::PathBuf;

use linch::{AgentConfig, McpTransportConfig, Principal, ProviderConfig};
use serde_json::json;
use url::Url;

use super::{Allowlists, Tenant};
use crate::error::{ErrorObject, code};

impl Tenant {
    /// Refuse `config` unless its namespace is this tenant's and every
    /// provider endpoint, workspace and MCP command it names is allowed.
    /// The workspace is replaced by the canonical path that was checked.
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
        if let Some(base_url) = base_url(&config.provider) {
            self.allow.admit_base_url(base_url)?;
        }
        if let Some(root) = config.workspace.take() {
            config.workspace = Some(self.allow.admit_workspace(root).await?);
        }
        for server in &config.mcp_servers {
            if let McpTransportConfig::Stdio { command, .. } = &server.transport
                && !self.allow.mcp_commands.contains(command)
            {
                let message = format!(
                    "MCP server {:?}: its command is not in the tenant's mcp_commands",
                    server.name
                );
                return Err(refused("mcp_command", message));
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
    fn admit_base_url(&self, base_url: &str) -> Result<(), ErrorObject> {
        let allowed = Url::parse(base_url).is_ok_and(|url| self.origins.contains(&url.origin()));
        if allowed {
            return Ok(());
        }
        let message = "provider base_url is not in the tenant's provider_base_urls, \
                       so the server's key may not be sent there";
        Err(refused("provider_base_url", message.into()))
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
