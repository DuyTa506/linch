//! Who a connection's client is and what it may ask for: [`Access`],
//! [`Tenant`]s with their [`Allowlists`], and the bearer [`Tokens`] that
//! name them.

use std::fmt;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use linch::PgPools;
use sha2::{Digest as _, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;
use tokio::sync::{OwnedSemaphorePermit, Semaphore, SemaphorePermit};

use crate::error::{ErrorObject, limit_reached};

mod admit;
mod allowlists;

pub use allowlists::{
    Allowlists, DEFAULT_MAX_AGENTS, DEFAULT_MAX_CONNECTIONS, DEFAULT_MAX_RUNS, McpCommand,
};

/// How a connection's client is trusted.
#[derive(Debug)]
pub enum Access {
    /// The client owns the server process, as on stdio: no token, no
    /// tenant, no allowlists. Its agents take their Postgres pools from
    /// these; pass one for every local connection of a process, so agents
    /// on one target share a pool.
    Local(Arc<PgPools>),
    /// The client already proved it acts for this place's tenant (the
    /// HTTP transport checked its bearer header), and the place, taken
    /// with [`Tenant::connect`] before the connection was accepted, is
    /// held until the connection ends.
    Tenant(ConnectionPlace),
    /// The client must first send `connection.auth` with one of these
    /// tokens; the connection then acts for the token's tenant.
    Login(Arc<Tokens>),
}

/// A tenant: the name its agents and runs belong to, what its agents may
/// use, its places for live runs, open agents and connections, and its
/// Postgres pools, shared by every connection that acts for it (share one
/// `Arc<Tenant>` among them). Its agents share pools with each other only,
/// so no other tenant sets their size. `Debug` shows the pools' sizes, no
/// DSN.
#[derive(Debug)]
pub struct Tenant {
    name: String,
    allow: Allowlists,
    runs: Arc<Semaphore>,
    agents: Arc<Semaphore>,
    connections: Arc<Semaphore>,
    pools: Arc<PgPools>,
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
        let places = |max: usize| Arc::new(Semaphore::new(max.min(Semaphore::MAX_PERMITS)));
        Ok(Self {
            name,
            runs: places(allow.max_runs),
            agents: places(allow.max_agents),
            connections: places(allow.max_connections),
            pools: Arc::new(PgPools::new()),
            allow,
        })
    }

    /// A place among the tenant's live runs, or `-32005`.
    pub(crate) fn run_place(&self) -> Result<OwnedSemaphorePermit, ErrorObject> {
        take(&self.runs, "runs", self.allow.max_runs)
    }

    /// A place among the tenant's open agents, or `-32005`.
    pub(crate) fn agent_place(&self) -> Result<OwnedSemaphorePermit, ErrorObject> {
        take(&self.agents, "agents", self.allow.max_agents)
    }

    /// Take a place among the tenant's connections for a client that
    /// proved it acts for the tenant, as [`Access::Tenant`] needs. A
    /// transport that knows the tenant before it accepts the connection
    /// takes it first, so it can refuse in its own terms (HTTP 429).
    ///
    /// Returns:
    ///     The place, given back when it drops.
    ///
    /// Raises:
    ///     ErrorObject: `-32005` with `data: {kind: "connections", max}`
    ///         when the tenant already holds `max_connections`.
    pub fn connect(self: Arc<Self>) -> Result<ConnectionPlace, ErrorObject> {
        let place = take(&self.connections, "connections", self.allow.max_connections)?;
        Ok(ConnectionPlace {
            tenant: self,
            _place: place,
        })
    }

    /// The tenant name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Where this tenant's agents take their Postgres pools.
    pub(crate) fn pools(&self) -> &Arc<PgPools> {
        &self.pools
    }

    /// What this tenant's agents may use.
    pub fn allowlists(&self) -> &Allowlists {
        &self.allow
    }
}

/// A connection's place among its tenant's connections, from
/// [`Tenant::connect`]; the place is given back when this drops.
#[derive(Debug)]
pub struct ConnectionPlace {
    pub(crate) tenant: Arc<Tenant>,
    _place: OwnedSemaphorePermit,
}

/// Socket connections that may wait for their login at once, across every
/// connection checked against one [`Tokens`]: each may buffer up to a
/// 16 MiB line for up to 10 s, so this bounds that to 512 MiB. One past it
/// gets `-32005` and closes without being read.
pub const MAX_PENDING_LOGINS: usize = 32;

/// Bearer tokens and the tenant each one names.
///
/// Tokens are kept only as SHA-256 digests, and a presented token is
/// compared with every one in constant time. They also count the socket
/// connections waiting for their login ([`MAX_PENDING_LOGINS`]). `Debug`
/// shows tenant names only.
pub struct Tokens(Vec<([u8; 32], Arc<Tenant>)>, Semaphore);

impl Default for Tokens {
    fn default() -> Self {
        Self(Vec::new(), Semaphore::new(MAX_PENDING_LOGINS))
    }
}

impl Tokens {
    /// No tokens: every presented token is refused.
    pub fn new() -> Self {
        Self::default()
    }

    /// A place among the connections waiting for their login, or
    /// `-32005`.
    pub(crate) fn pending_place(&self) -> Result<SemaphorePermit<'_>, ErrorObject> {
        (self.1.try_acquire()).map_err(|_| limit_reached("logins", MAX_PENDING_LOGINS))
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

fn take(
    places: &Arc<Semaphore>,
    kind: &str,
    max: usize,
) -> Result<OwnedSemaphorePermit, ErrorObject> {
    Arc::clone(places)
        .try_acquire_owned()
        .map_err(|_| limit_reached(kind, max))
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
    /// An entry of a URL or DSN allowlist cannot be read: not an `http`
    /// or `https` URL, or not a Postgres DSN.
    #[error("{list}[{index}] is not a valid entry")]
    Url {
        /// The allowlist: `provider_base_urls`, `mcp_urls` or
        /// `postgres_dsns`.
        list: &'static str,
        /// Its position in the list.
        index: usize,
    },
    /// An `mcp_commands` entry lets a client set `PATH` or a variable that
    /// decides what code a program loads.
    #[error("mcp_commands[{index}].env may not list {name}: it decides what code the server runs")]
    McpEnv {
        /// The entry's position in the list.
        index: usize,
        /// The refused variable.
        name: String,
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
