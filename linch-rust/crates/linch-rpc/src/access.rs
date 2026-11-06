//! Who a connection's client is and what it may ask for: [`Access`],
//! [`Tenant`]s with their [`Allowlists`], and the bearer [`Tokens`] that
//! name them.

use std::fmt;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use sha2::{Digest as _, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;
use url::{Origin, Url};

mod admit;

/// How a connection's client is trusted.
#[derive(Debug, Clone)]
pub enum Access {
    /// The client owns the server process, as on stdio: no token, no
    /// tenant, no allowlists.
    Local,
    /// The client already proved it acts for this tenant (the HTTP
    /// transport checked its bearer header).
    Tenant(Arc<Tenant>),
    /// The client must first send `connection.auth` with one of these
    /// tokens; the connection then acts for the token's tenant.
    Login(Arc<Tokens>),
}

/// A tenant: the name its agents and runs belong to, and what its agents
/// may use.
#[derive(Debug)]
pub struct Tenant {
    name: String,
    allow: Allowlists,
}

impl Tenant {
    /// A tenant named `name`, allowed what `allow` lists.
    ///
    /// Raises:
    ///     AccessError: If `name` is not 1-64 ASCII letters, digits, `.`,
    ///         `_` or `-`. It never holds `/`, so the namespace rule
    ///         (`name` or `name/...`) cannot reach another tenant's.
    pub fn new(name: impl Into<String>, allow: Allowlists) -> Result<Self, AccessError> {
        let name = name.into();
        let valid = (1..=64).contains(&name.len())
            && (name.bytes()).all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b));
        if !valid {
            return Err(AccessError::TenantName { name });
        }
        Ok(Self { name, allow })
    }

    /// The tenant name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// What this tenant's agents may use.
    pub fn allowlists(&self) -> &Allowlists {
        &self.allow
    }
}

/// What a tenant's `agent.open` may ask for. Each list allows nothing when
/// empty.
#[derive(Debug, Default)]
pub struct Allowlists {
    origins: Vec<Origin>,
    workspace_roots: Vec<PathBuf>,
    mcp_commands: Vec<String>,
}

impl Allowlists {
    /// Build the allowlists, resolving each workspace root.
    ///
    /// Blocks on the file system (each root is canonicalized), so call it
    /// at startup.
    ///
    /// Args:
    ///     provider_base_urls: `http` or `https` URLs. A provider
    ///         `base_url` is allowed when its scheme, host and port equal
    ///         one of these; the path does not matter.
    ///     workspace_roots: Existing directories. A `workspace` is allowed
    ///         when its canonical path is one of them or lies under one.
    ///     mcp_commands: Programs an MCP `stdio` transport may run, each
    ///         compared with `command` exactly.
    ///
    /// Raises:
    ///     AccessError: If a URL is not `http` or `https`, or a root cannot
    ///         be resolved.
    pub fn new(
        provider_base_urls: &[String],
        workspace_roots: &[PathBuf],
        mcp_commands: &[String],
    ) -> Result<Self, AccessError> {
        let origins = (provider_base_urls.iter().enumerate())
            .map(|(index, url)| match Url::parse(url) {
                Ok(url) if matches!(url.scheme(), "http" | "https") => Ok(url.origin()),
                _ => Err(AccessError::BaseUrl { index }),
            })
            .collect::<Result<_, _>>()?;
        let workspace_roots = (workspace_roots.iter())
            .map(|root| {
                std::fs::canonicalize(root).map_err(|source| AccessError::WorkspaceRoot {
                    root: root.clone(),
                    source,
                })
            })
            .collect::<Result<_, _>>()?;
        Ok(Self {
            origins,
            workspace_roots,
            mcp_commands: mcp_commands.to_vec(),
        })
    }
}

/// Bearer tokens and the tenant each one names.
///
/// Tokens are kept only as SHA-256 digests, and a presented token is
/// compared with every one in constant time. `Debug` shows tenant names
/// only.
#[derive(Default)]
pub struct Tokens(Vec<([u8; 32], Arc<Tenant>)>);

impl Tokens {
    /// No tokens: every presented token is refused.
    pub fn new() -> Self {
        Self::default()
    }

    /// Accept `token` for `tenant`.
    ///
    /// Raises:
    ///     AccessError: If `token` is blank, or is already accepted (for
    ///         any tenant).
    pub fn insert(&mut self, token: &str, tenant: Arc<Tenant>) -> Result<(), AccessError> {
        let name = tenant.name().to_owned();
        if token.trim().is_empty() {
            return Err(AccessError::BlankToken { tenant: name });
        }
        let digest = digest(token);
        if self.0.iter().any(|(known, _)| *known == digest) {
            return Err(AccessError::DuplicateToken { tenant: name });
        }
        self.0.push((digest, tenant));
        Ok(())
    }

    /// The tenant `presented` belongs to, if any.
    pub fn tenant(&self, presented: &str) -> Option<Arc<Tenant>> {
        let presented = digest(presented);
        let mut found = None;
        // Every digest is compared, so the time does not tell which one
        // matched or how much of it.
        for (known, tenant) in &self.0 {
            if bool::from(known.ct_eq(&presented)) {
                found = Some(tenant);
            }
        }
        found.cloned()
    }

    /// Whether no token is accepted.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for Tokens {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let tenants: Vec<&str> = self.0.iter().map(|(_, tenant)| tenant.name()).collect();
        f.debug_struct("Tokens").field("tenants", &tenants).finish()
    }
}

fn digest(token: &str) -> [u8; 32] {
    Sha256::digest(token.as_bytes()).into()
}

/// A tenant, token or allowlist could not be set up. No message holds a
/// token.
#[derive(Debug, Error)]
pub enum AccessError {
    /// A tenant name is empty, too long or has other characters.
    #[error("tenant name {name:?} must be 1-64 ASCII letters, digits, '.', '_' or '-'")]
    TenantName {
        /// The rejected name.
        name: String,
    },
    /// An entry of `provider_base_urls` is not an `http` or `https` URL.
    #[error("provider_base_urls[{index}] is not an http or https URL")]
    BaseUrl {
        /// Its position in the list.
        index: usize,
    },
    /// A workspace root does not exist or cannot be resolved.
    #[error("workspace root {} cannot be resolved: {source}", root.display())]
    WorkspaceRoot {
        /// The root as configured.
        root: PathBuf,
        /// Why canonicalizing it failed.
        #[source]
        source: io::Error,
    },
    /// A token is empty or blank.
    #[error("a token of tenant {tenant:?} is blank")]
    BlankToken {
        /// The tenant it was given for.
        tenant: String,
    },
    /// The same token is given twice, which would make its tenant
    /// ambiguous.
    #[error("a token of tenant {tenant:?} is already given")]
    DuplicateToken {
        /// The tenant of the second occurrence.
        tenant: String,
    },
}

#[cfg(test)]
mod tests;
