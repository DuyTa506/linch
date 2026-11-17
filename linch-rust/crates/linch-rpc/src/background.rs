//! [`BackgroundAgents`]: the [`AgentFactory`] a server's worker opens
//! scheduled and queued runs with, checking each stored definition against
//! its tenant's allowlists again at every attempt.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::num::NonZeroU32;
use std::sync::Arc;

use futures::FutureExt;
use futures::future::BoxFuture;
use linch::{AgentConfig, Hooks, PgPools, StoreConfig, ToolRegistry};
use linch_worker::{AgentFactory, FactoryError, JobAgent};

use crate::access::Tenant;
use crate::error::ErrorObject;
use crate::schedule_tools::{Binding, Bound};

/// Opens the agent of a job that carries a stored agent definition (an
/// agent config serialized by `schedule.create` or `job.enqueue`).
///
/// - A namespace under a tenant (its first `/`-separated segment is the
///   tenant's name) is checked against that tenant's allowlists as an
///   `agent.open` would be, at every attempt, so a tightened allowlist
///   stops a schedule's next fire. A refused definition fails its job.
///   Each run takes one of the tenant's `max_runs` places for as long as
///   the attempt lasts; with none free the job waits, without counting a
///   failure. The tenant's Postgres pools are used.
/// - A namespace under a local root ([`Self::local`]) is trusted as a
///   stdio client is: no checks, no caps.
/// - Any other namespace is refused; the worker does not even take such
///   jobs, since it serves only [`Self::roots`].
///
/// A definition with `schedule_tools` gets them bound as `agent.open`
/// binds them (same registry position, so the same contract), to the
/// definition text as stored and the tenant's caps; under a local root it
/// is refused, as a local connection's open is.
///
/// The definition's store DSN is replaced by this factory's DSN: the
/// worker's database and schema, where the job was found. The tenant's
/// `postgres_dsns` are not consulted for it; the operator chose it.
pub struct BackgroundAgents {
    dsn: String,
    tenants: HashMap<String, Arc<Tenant>>,
    local: HashSet<String>,
    local_pools: Arc<PgPools>,
}

impl BackgroundAgents {
    /// A factory whose agents store their runs through `dsn`, with no
    /// tenants and no local roots yet.
    pub fn new(dsn: impl Into<String>) -> Self {
        Self {
            dsn: dsn.into(),
            tenants: HashMap::new(),
            local: HashSet::new(),
            local_pools: Arc::new(PgPools::new()),
        }
    }

    /// Serve the namespaces of `tenant`, checked against its allowlists
    /// and counted against its run cap. Share the `Arc` the listeners use,
    /// so its places and pools are the same.
    #[must_use]
    pub fn tenant(mut self, tenant: Arc<Tenant>) -> Self {
        self.tenants.insert(tenant.name().to_owned(), tenant);
        self
    }

    /// Serve the namespaces under `root` (`root` itself and `root/...`)
    /// without checks, as a local client's agents are.
    #[must_use]
    pub fn local(mut self, root: impl Into<String>) -> Self {
        self.local.insert(root.into());
        self
    }

    /// The cap on unfinished jobs of each tenant with `max_queued_jobs`,
    /// for [`linch_worker::Worker::job_caps`]: a schedule's fire there is
    /// skipped at the cap, as `job.enqueue` is refused. A tenant without it,
    /// and a local root, have none.
    pub fn job_caps(&self) -> Vec<linch_worker::Cap> {
        let mut caps: Vec<linch_worker::Cap> = (self.tenants.values())
            .filter_map(|tenant| {
                let max = tenant.allowlists().max_queued_jobs?;
                let root = tenant.name().to_owned();
                Some(linch_worker::Cap { root, max })
            })
            .collect();
        caps.sort_by(|a, b| a.root.cmp(&b.root));
        caps
    }

    /// Every root this factory serves: the tenants' names and the local
    /// roots, for [`linch_worker::Worker::definitions`].
    pub fn roots(&self) -> Vec<String> {
        let mut roots: Vec<String> = (self.tenants.keys().chain(&self.local)).cloned().collect();
        roots.sort();
        roots.dedup();
        roots
    }

    async fn open_job(&self, namespace: &str, definition: &str) -> Result<JobAgent, FactoryError> {
        let refused = FactoryError::Refused;
        let mut config: AgentConfig = serde_json::from_str(definition)
            .map_err(|error| refused(format!("the agent definition does not decode: {error}")))?;
        if config.namespace != namespace {
            return Err(refused(format!(
                "the agent definition is for namespace {:?}, not {namespace:?}",
                config.namespace
            )));
        }
        let StoreConfig::Postgres {
            max_connections,
            lease_ttl_ms,
            ..
        } = config.stores
        else {
            return Err(refused(
                "the agent definition has no Postgres stores".into(),
            ));
        };
        let root = namespace.split('/').next().unwrap_or_default();
        let scheduling = match (config.schedule_tools, self.tenants.get(root)) {
            (false, _) => None,
            (true, Some(tenant)) => Some(Arc::clone(tenant)),
            (true, None) => {
                return Err(refused(
                    "schedule_tools needs a tenant whose allowlists give it schedules".into(),
                ));
            }
        };
        let (pools, place, max_connections) = if let Some(tenant) = self.tenants.get(root) {
            // The store is the worker's, not the client's, so the DSN check
            // does not apply; everything else does.
            config.stores = StoreConfig::Memory {};
            (tenant.admit(&mut config).await).map_err(|error| refused(error.message))?;
            let place = tenant
                .run_place()
                .map_err(|error| FactoryError::Busy(error.message))?;
            let cap = tenant.allowlists().max_pool_connections;
            let size: NonZeroU32 = max_connections
                .unwrap_or_else(linch::postgres::default_max_connections)
                .min(cap);
            (Arc::clone(tenant.pools()), Some(place), Some(size))
        } else if self.local.contains(root) {
            (Arc::clone(&self.local_pools), None, max_connections)
        } else {
            return Err(refused(format!(
                "no tenant or local root serves {namespace:?}"
            )));
        };
        config.stores = StoreConfig::Postgres {
            dsn: self.dsn.clone(),
            max_connections,
            lease_ttl_ms,
        };
        let tools = Arc::new(ToolRegistry::new());
        // First in the registry, as `agent.open` puts them, so the contract
        // is the one the definition was made with.
        let binding = match &scheduling {
            Some(_) => Some(Binding::register(&tools).map_err(|error| refused(error.message))?),
            None => None,
        };
        let opened = (config
            .open_with(tools, Arc::new(Hooks::default()), &pools)
            .await)
            .map_err(|error| {
                let object = ErrorObject::from(&error);
                let object = match self.tenants.get(root) {
                    Some(tenant) => tenant.open_error(object),
                    None => object,
                };
                FactoryError::Unavailable(object.message)
            })?;
        if let (Some(binding), Some(tenant)) = (binding, scheduling) {
            binding.bind(Bound {
                pool: (opened.pool().cloned()).expect("a background agent has Postgres stores"),
                namespace: namespace.to_owned(),
                // As stored, not encoded again: its digest names its schedules.
                definition: definition.to_owned(),
                tenant,
                migrated: Arc::default(),
            });
        }
        let agent = opened.agent().clone();
        let close = async move {
            opened.close().await;
            drop(place);
        };
        Ok(JobAgent::new(agent).on_close(close.boxed()))
    }
}

impl AgentFactory for BackgroundAgents {
    fn open<'a>(
        &'a self,
        namespace: &'a str,
        definition: &'a str,
    ) -> BoxFuture<'a, Result<JobAgent, FactoryError>> {
        self.open_job(namespace, definition).boxed()
    }
}

impl fmt::Debug for BackgroundAgents {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BackgroundAgents")
            .field("roots", &self.roots())
            .finish_non_exhaustive()
    }
}
