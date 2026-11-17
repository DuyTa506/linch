//! [`Allowlists`]: what a tenant's `agent.open` may ask for.

use std::num::NonZeroU32;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

use linch::{ExecutionConfig, PermissionMode, ReliabilityPolicy, SystemConfig, SystemSection};
use serde::Deserialize;
use sqlx::postgres::{PgConnectOptions, PgSslMode};
use url::{Origin, Url};

use super::AccessError;

/// Live runs a tenant drives at once unless its allowlists say otherwise:
/// each streams from a provider on the server's key, so this bounds what
/// one tenant spends of the server's rate limits and memory.
pub const DEFAULT_MAX_RUNS: usize = 16;

/// Connections a tenant holds at once unless its allowlists say
/// otherwise: a socket connection may buffer a 16 MiB line, and a
/// WebSocket one a 64 MiB message (`linch-server`'s `MAX_MESSAGE_BYTES`)
/// plus its 64 MiB `String` copy, 128 MiB at peak, so this bounds one
/// tenant to about 1 GiB of buffers.
pub const DEFAULT_MAX_CONNECTIONS: usize = 8;

/// Agents a tenant keeps open at once unless its allowlists say
/// otherwise: each holds its config and tools in the server's memory and
/// runs its MCP stdio servers as child processes until it is closed.
pub const DEFAULT_MAX_AGENTS: usize = 16;

/// The shortest time between two fires of a tenant's schedule unless its
/// allowlists say otherwise: every fire is a run on the server's provider
/// key, so this bounds how fast one schedule spends it.
pub const DEFAULT_MIN_SCHEDULE_INTERVAL: Duration = Duration::from_secs(60);

/// Bytes of cached tool results all of a tenant's namespaces may hold
/// together unless its allowlists say otherwise: with Postgres stores the
/// rows are the operator's database, with memory stores each agent's own
/// store holds up to this much.
pub const DEFAULT_TOOL_CACHE_BYTES: u64 = 64 * 1024 * 1024;

/// What a tenant may ask for, built with the methods below. A new value
/// allows the memory stores, the permission modes `default` and
/// `accept_edits`, the live providers at their public endpoints, the
/// `local` execution backend (which matters only with exec tools), and
/// nothing else: no `Bash`, no workspace; and at most
/// [`DEFAULT_MAX_RUNS`] live runs, [`DEFAULT_MAX_AGENTS`] open agents and
/// [`DEFAULT_MAX_CONNECTIONS`] connections at once, and Postgres pools of
/// at most [`linch::postgres::default_max_connections`] connections.
#[derive(Debug)]
pub struct Allowlists {
    pub(super) scripted_provider: bool,
    pub(super) exec_tools: bool,
    pub(super) tool_cache: bool,
    pub(super) tool_cache_shared: bool,
    pub(super) tool_cache_bytes: u64,
    pub(super) max_runs: usize,
    pub(super) max_agents: usize,
    pub(super) max_connections: usize,
    pub(crate) max_pool_connections: NonZeroU32,
    pub(crate) max_provider_concurrency: Option<NonZeroU32>,
    pub(crate) max_schedules: Option<u64>,
    pub(crate) min_schedule_interval: Duration,
    pub(crate) max_queued_jobs: Option<u64>,
    pub(super) memory: bool,
    pub(super) max_memories: Option<u64>,
    pub(super) provider_origins: Vec<Origin>,
    pub(super) permission_modes: Vec<PermissionMode>,
    pub(super) workspace_roots: Vec<PathBuf>,
    pub(super) postgres: Vec<PgTarget>,
    pub(super) mcp_commands: Vec<McpCommand>,
    pub(super) mcp_origins: Vec<Origin>,
    pub(super) execution: Vec<ExecutionConfig>,
    pub(super) system_sections: Vec<SystemSection>,
    pub(super) required_policy: Option<ReliabilityPolicy>,
}

/// One MCP `stdio` server a tenant may start: its program and arguments,
/// which a transport must match exactly, and the environment variables a
/// transport may set for it.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct McpCommand {
    /// The program, compared with the transport's `command`.
    pub command: String,
    /// The arguments, compared with the transport's `args` as a whole
    /// list; none when absent.
    #[serde(default)]
    pub args: Vec<String>,
    /// Names of the variables the transport's `env` may set; none when
    /// absent. `PATH`, `HOME` and variables known to decide what code a
    /// program loads or which config files it reads (`LD_*`, `DYLD_*`,
    /// `PYTHONPATH`, `NODE_OPTIONS`, `npm_config_*`, `PIP_*`, ...) cannot
    /// be listed. That list is best effort: a program may read others, so
    /// list only names whose effect on the program you know.
    #[serde(default)]
    pub env: Vec<String>,
}

/// Variables that choose which code a program loads or runs first, or
/// where it finds the rc and config files that can (`HOME`, as for an
/// `.npmrc` whose `node-options` `npx` obeys); an [`McpCommand`] may not
/// let a client set them. Best effort: these are the well-known ones.
const CODE_LOADING_ENV: [&str; 23] = [
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "BASH_ENV",
    "ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PERL5LIB",
    "PERLLIB",
    "PERL5OPT",
    "RUBYLIB",
    "RUBYOPT",
    "GEM_HOME",
    "GEM_PATH",
    "CLASSPATH",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "GCONV_PATH",
];

/// Prefixes of the same: the dynamic loaders' variables, and the settings
/// pip and uv read (an index URL, a config file).
const CODE_LOADING_PREFIXES: [&str; 4] = ["LD_", "DYLD_", "PIP_", "UV_"];

fn loads_code(name: &str) -> bool {
    // npm reads `npm_config_*` in any case.
    let npm = (name.get(..11)).is_some_and(|head| head.eq_ignore_ascii_case("npm_config_"));
    npm || CODE_LOADING_ENV.contains(&name)
        || (CODE_LOADING_PREFIXES.iter()).any(|prefix| name.starts_with(prefix))
}

/// Where a Postgres DSN connects, as whom, and how strictly it asks for
/// TLS: what an allowed DSN must match.
#[derive(Debug)]
pub(super) struct PgTarget {
    host: String,
    port: u16,
    socket: Option<PathBuf>,
    database: Option<String>,
    user: String,
    options: Option<String>,
    /// The files the server would read for TLS, as named in the DSN:
    /// `sslrootcert`, `sslcert` and `sslkey`.
    root_cert: Option<String>,
    client_cert: Option<String>,
    client_key: Option<String>,
    /// The `sslmode` as a rank, from `disable` (0) to `verify-full` (5).
    tls: u8,
}

impl PgTarget {
    /// Read `dsn` the way the store connects with it, so a `?host=` or
    /// `?user=` override counts, a DSN without a user gets the server's
    /// own (`PGUSER`, else the process's user), and one without an
    /// `sslmode` the server's default (`PGSSLMODE`, else `prefer`).
    pub(super) fn parse(dsn: &str) -> Option<Self> {
        let options = PgConnectOptions::from_str(dsn).ok()?;
        let mut target = Self {
            host: options.get_host().to_owned(),
            port: options.get_port(),
            socket: options.get_socket().cloned(),
            database: options.get_database().map(str::to_owned),
            user: options.get_username().to_owned(),
            options: options.get_options().map(str::to_owned),
            root_cert: None,
            client_cert: None,
            client_key: None,
            tls: match options.get_ssl_mode() {
                PgSslMode::Disable => 0,
                PgSslMode::Allow => 1,
                PgSslMode::Prefer => 2,
                PgSslMode::Require => 3,
                PgSslMode::VerifyCa => 4,
                PgSslMode::VerifyFull => 5,
            },
        };
        // sqlx has no getters for these, so read the query as it does:
        // under each of its names, the last one winning.
        for (key, value) in Url::parse(dsn).ok()?.query_pairs() {
            let file = match &*key {
                "sslrootcert" | "ssl-root-cert" | "ssl-ca" => &mut target.root_cert,
                "sslcert" | "ssl-cert" => &mut target.client_cert,
                "sslkey" | "ssl-key" => &mut target.client_key,
                _ => continue,
            };
            *file = Some(value.into_owned());
        }
        Some(target)
    }

    /// Whether this entry allows `dsn`: the same server, database, user,
    /// options and TLS files, and an `sslmode` this strict or stricter.
    pub(super) fn allows(&self, dsn: &Self) -> bool {
        // Taken apart, so a field added later cannot miss the check.
        let Self {
            host,
            port,
            socket,
            database,
            user,
            options,
            root_cert,
            client_cert,
            client_key,
            tls,
        } = self;
        (host, port, socket, database) == (&dsn.host, &dsn.port, &dsn.socket, &dsn.database)
            && (user, options) == (&dsn.user, &dsn.options)
            && (root_cert, client_cert, client_key)
                == (&dsn.root_cert, &dsn.client_cert, &dsn.client_key)
            && dsn.tls >= *tls
    }
}

impl Default for Allowlists {
    fn default() -> Self {
        Self::new()
    }
}

impl Allowlists {
    /// The defaults described on [`Allowlists`].
    pub fn new() -> Self {
        Self {
            scripted_provider: false,
            exec_tools: false,
            tool_cache: false,
            tool_cache_shared: false,
            tool_cache_bytes: DEFAULT_TOOL_CACHE_BYTES,
            max_runs: DEFAULT_MAX_RUNS,
            max_agents: DEFAULT_MAX_AGENTS,
            max_connections: DEFAULT_MAX_CONNECTIONS,
            max_pool_connections: linch::postgres::default_max_connections(),
            max_provider_concurrency: None,
            max_schedules: None,
            min_schedule_interval: DEFAULT_MIN_SCHEDULE_INTERVAL,
            max_queued_jobs: None,
            memory: false,
            max_memories: None,
            provider_origins: Vec::new(),
            permission_modes: vec![PermissionMode::Default, PermissionMode::AcceptEdits],
            workspace_roots: Vec::new(),
            postgres: Vec::new(),
            mcp_commands: Vec::new(),
            mcp_origins: Vec::new(),
            execution: vec![ExecutionConfig::Local {}],
            system_sections: Vec::new(),
            required_policy: None,
        }
    }

    /// Whether a tenant's `spec.tool_cache` is allowed. Off by default: the
    /// cache keeps results in the server's memory or its database, bounded
    /// to 1024 entries a namespace, a one-hour ttl, 1 MiB a result and
    /// [`Self::tool_cache_bytes`] for all the tenant's namespaces together.
    /// A tenant's cache must also have `spec.redaction` over tool results,
    /// since the cache stores what the tool returned after it.
    pub fn tool_cache(mut self, allowed: bool) -> Self {
        self.tool_cache = allowed;
        self
    }

    /// Whether a tenant's `tool_cache` may share entries across the
    /// sessions of its namespace (`scope: "namespace"`). Off by default: a
    /// read tool may answer per session, and one session's result must not
    /// reach another. Only matters with [`Self::tool_cache`].
    pub fn tool_cache_shared(mut self, allowed: bool) -> Self {
        self.tool_cache_shared = allowed;
        self
    }

    /// Most bytes of cached results all of a tenant's namespaces may hold
    /// together; [`DEFAULT_TOOL_CACHE_BYTES`] (64 MiB) when not set. The
    /// store enforces it on every put by dropping the tenant's oldest
    /// entries; a result is stored as JSON, so its length counts.
    pub fn tool_cache_bytes(mut self, bytes: u64) -> Self {
        self.tool_cache_bytes = bytes;
        self
    }

    /// Whether the `scripted` provider is allowed (for tests and evals).
    pub fn scripted_provider(mut self, allowed: bool) -> Self {
        self.scripted_provider = allowed;
        self
    }

    /// Endpoints a provider `base_url` may name, so they receive the
    /// server's key: `http` or `https` URLs, matched on scheme, host and
    /// port; the path does not matter.
    ///
    /// Raises:
    ///     AccessError: If an entry is not an `http` or `https` URL.
    pub fn provider_base_urls(mut self, urls: &[String]) -> Result<Self, AccessError> {
        self.provider_origins = origins("provider_base_urls", urls)?;
        Ok(self)
    }

    /// How many runs the tenant's connections may drive at once, all
    /// together: a `run.start`, `run.resume`, `run.approve` or `run.answer` past it is
    /// refused with `-32005`. A run holds its place until its `run.end`; a
    /// run that suspends with background subagents running keeps it until
    /// they end, or until its approve or resume takes it over.
    pub fn max_runs(mut self, max: usize) -> Self {
        self.max_runs = max;
        self
    }

    /// How many agents the tenant's connections may keep open at once, all
    /// together: an `agent.open` past it is refused with `-32005` before
    /// anything starts. An agent holds its place until `agent.close`, or
    /// its connection's end, has closed its MCP servers.
    pub fn max_agents(mut self, max: usize) -> Self {
        self.max_agents = max;
        self
    }

    /// How many connections may act for the tenant at once; one past it
    /// is refused ([`Tenant::connect`](super::Tenant::connect) fails with
    /// `-32005`).
    pub fn max_connections(mut self, max: usize) -> Self {
        self.max_connections = max;
        self
    }

    /// The largest Postgres pool the tenant's agents may ask for with
    /// `stores.max_connections`; an `agent.open` asking for more is refused
    /// (`kind: pool_connections`), and one that asks for no size gets the
    /// default size or this, whichever is smaller. The tenant's agents
    /// share pools only with each other, one per target, so this bounds
    /// what the tenant holds of the database's connections on each target.
    pub fn max_pool_connections(mut self, max: NonZeroU32) -> Self {
        self.max_pool_connections = max;
        self
    }

    /// The most provider calls the tenant's runs make at once, all together
    /// and on each provider key and endpoint: its agents share one gate
    /// per key and endpoint (see `max_provider_concurrency` of
    /// [`AgentConfig`](linch::AgentConfig)), so no number of agents or
    /// runs goes past it. A config asking for more is refused
    /// (`kind: provider_concurrency`); one that asks for none gets this.
    /// `None` (the default) caps nothing. The gate is the server
    /// process's: a worker process, or a second server, has its own.
    pub fn max_provider_concurrency(mut self, max: Option<NonZeroU32>) -> Self {
        self.max_provider_concurrency = max;
        self
    }

    /// How many schedules the tenant may keep, paused ones included, in
    /// each database its agents store in; `None` (the default) lets it
    /// create none (`schedule.create` is refused, `kind: schedules`). One
    /// past the cap is refused with `-32005`.
    pub fn max_schedules(mut self, max: Option<u64>) -> Self {
        self.max_schedules = max;
        self
    }

    /// The shortest time a schedule may leave between two fires, an
    /// interval or the closest fires of a cron pattern;
    /// [`DEFAULT_MIN_SCHEDULE_INTERVAL`] unless set.
    pub fn min_schedule_interval(mut self, min: Duration) -> Self {
        self.min_schedule_interval = min;
        self
    }

    /// Whether a config may turn on long-term `memory`, which stores what
    /// the model saves in the tenant's database, namespaces and scope
    /// included; off (the default) refuses it (`kind: memory`). It also
    /// needs [`Self::max_memories`]: a config that turns memory on for a
    /// tenant with no cap is refused too.
    pub fn memory(mut self, allowed: bool) -> Self {
        self.memory = allowed;
        self
    }

    /// How many memories the tenant may keep, in all its namespaces of a
    /// database. A config's `memory.max_memories` is bounded by it (the
    /// smaller holds, and a config that names none gets it), so a save past
    /// it fails for the model with `limit_reached`. A config's
    /// `max_memories_per_scope` is bounded by the cap the same way, and a
    /// config that names none gives each scope a quarter of it. `None` (the
    /// default) is no cap, which refuses every config with `memory`.
    pub fn max_memories(mut self, max: Option<u64>) -> Self {
        self.max_memories = max;
        self
    }

    /// How many jobs (one-off background runs and schedule fires) the
    /// tenant may have queued, running or waiting to retry at once in each
    /// database, counted when `job.enqueue` adds one; `None` (the
    /// default) lets it enqueue none (`kind: background_runs`). A fire of
    /// its schedules at the cap is skipped, never failed; with `None`,
    /// fires have no cap.
    pub fn max_queued_jobs(mut self, max: Option<u64>) -> Self {
        self.max_queued_jobs = max;
        self
    }

    /// Whether a workspace agent gets `Bash`, which runs commands through
    /// its execution backend (see [`Self::execution`]; unconfined, as the
    /// server's user, with the default `local`), with `BashOutput` and
    /// `BashKill` for its background commands. Off by default: the agent
    /// then has none of the three, whatever its config's `bash` says.
    pub fn exec_tools(mut self, allowed: bool) -> Self {
        self.exec_tools = allowed;
        self
    }

    /// The execution backends a config may choose, each with the policy
    /// the operator set, replacing the default `[local]`. A config's
    /// `execution` must equal an entry exactly, so a client cannot pick
    /// its own image, network or limits; a config without one gets the
    /// first entry. So `[sandbox]` holds every agent of the tenant to that
    /// sandbox and denies the local backend, and an empty list refuses
    /// every config.
    pub fn execution(mut self, backends: &[ExecutionConfig]) -> Self {
        self.execution = backends.to_vec();
        self
    }

    /// System prompt sections the operator sets for every agent of the
    /// tenant, sent with the tenant's own, sorted by their `order` (a tie
    /// goes to these). Each `agent.open` and each stored background config
    /// has them added at admission, and a section of the config with the
    /// same name is replaced by the operator's, so a tenant can neither
    /// remove nor change one, and re-admitting a config that already holds
    /// them adds them once. `replace_defaults` does not remove them, and
    /// the bounds on the tenant's own sections (16 sections of at most
    /// 16 KiB, 64 KiB with `append`) do not count them.
    ///
    /// Changing them changes the run contract of the tenant's agents, so
    /// their in-flight runs are refused on resume (`contract mismatch`)
    /// until they finish or are started again. A section that is not
    /// cacheable must have an `order` after every cacheable block of the
    /// tenant's, or the agent does not open.
    ///
    /// Raises:
    ///     AccessError: If a name is blank or repeated, or a text is blank.
    pub fn system_sections(mut self, sections: &[SystemSection]) -> Result<Self, AccessError> {
        let config = SystemConfig {
            sections: sections.to_vec(),
            ..SystemConfig::default()
        };
        if let Some(problem) = config.problem(&[]) {
            return Err(AccessError::SystemSections { problem });
        }
        self.system_sections = sections.to_vec();
        Ok(self)
    }

    /// The reliability policy every agent of the tenant must carry: the
    /// operator's prerequisites and completion gates, which a config's own
    /// `spec.policy` may add to but whose rules it cannot remove or
    /// redefine. `agent.open` merges it into the config before the agent is
    /// built, so it is part of the run contract: a prerequisite takes the
    /// union of both sides' requirements, a gate the operator named cannot
    /// be redefined (the same gate, or one of another name, may be added),
    /// and the corrections allowed are the operator's when the config sets
    /// none, else the lesser of the two caps. A rule that names a tool the
    /// agent lacks refuses the open rather than being dropped, with an error
    /// that does not name the tool. Absent (the default), the tenant's
    /// policy stands alone.
    ///
    /// What it enforces is the order of the model's calls, counted from the
    /// agent's own tool results. It does not bind the tenant's host: a
    /// client's host tool registered under a required name, a
    /// `pre_tool_use` hook that answers the call, a `post_tool_use`
    /// replacement or a cache hit can satisfy a prerequisite, as can any
    /// tool that does the same work (`Bash`). It is not a security
    /// boundary; keep permissions and the sandbox for that.
    pub fn required_policy(mut self, policy: Option<ReliabilityPolicy>) -> Self {
        self.required_policy = policy;
        self
    }

    /// The permission modes a config may choose, replacing the default
    /// `default` and `accept_edits`. Only with `skip_dangerous` among them
    /// may a config carry `auto_allow` entries or rules that allow, or turn
    /// on `tasks` or `deep_agent` (which auto-allow the task tools), since
    /// each lets calls run without asking, as `skip_dangerous` does.
    pub fn permission_modes(mut self, modes: &[PermissionMode]) -> Self {
        self.permission_modes = modes.to_vec();
        self
    }

    /// Directories a `workspace`, a `permissions.project_root` and, with
    /// skills or subagents on, a `config_dir` may be, or lie under,
    /// compared on the canonical path. Without one a config can name none
    /// of them, so it keeps the default policy. Blocks on the file system,
    /// so call it at startup.
    ///
    /// Raises:
    ///     AccessError: If a root does not exist or cannot be resolved.
    pub fn workspace_roots(mut self, roots: &[PathBuf]) -> Result<Self, AccessError> {
        self.workspace_roots = (roots.iter())
            .map(|root| {
                std::fs::canonicalize(root).map_err(|source| AccessError::WorkspaceRoot {
                    root: root.clone(),
                    source,
                })
            })
            .collect::<Result<_, _>>()?;
        Ok(self)
    }

    /// Postgres servers a store DSN may connect to, and as whom: a DSN is
    /// allowed when its host, port (or socket), database, user, startup
    /// `options` and TLS files (`sslrootcert`, `sslcert`, `sslkey`) all
    /// equal an entry's, and its `sslmode` is the entry's or stricter
    /// (`disable` < `allow` < `prefer` < `require` < `verify-ca` <
    /// `verify-full`). An entry without a user stands for the server's
    /// own (`PGUSER`, else the process's user), whose password the server
    /// may hold (`PGPASSWORD`, `.pgpass`); an entry without options or
    /// files allows none, so a client cannot pick a role, a `search_path`,
    /// or a certificate or key file on the server the entry does not name.
    /// An entry, or a DSN, without `sslmode` has the server's default
    /// (`PGSSLMODE`, else `prefer`), so an entry that asks for TLS keeps a
    /// client from turning it off or skipping the certificate check. The
    /// password is the client's.
    ///
    /// Raises:
    ///     AccessError: If an entry is not a valid DSN.
    pub fn postgres_dsns(mut self, dsns: &[String]) -> Result<Self, AccessError> {
        self.postgres = (dsns.iter().enumerate())
            .map(|(index, dsn)| {
                PgTarget::parse(dsn).ok_or(AccessError::Url {
                    list: "postgres_dsns",
                    index,
                })
            })
            .collect::<Result<_, _>>()?;
        Ok(self)
    }

    /// MCP `stdio` servers a transport may start: one whose `command` and
    /// `args` equal an entry's, setting in `env` only names that entry
    /// lists. A bare program name is looked up on the server's `PATH`.
    ///
    /// Raises:
    ///     AccessError: If an entry lists `PATH`, `HOME` or another
    ///         variable known to decide what code a program loads.
    pub fn mcp_commands(mut self, commands: &[McpCommand]) -> Result<Self, AccessError> {
        for (index, entry) in commands.iter().enumerate() {
            if let Some(name) = entry.env.iter().find(|name| loads_code(name)) {
                let name = name.clone();
                return Err(AccessError::McpEnv { index, name });
            }
        }
        self.mcp_commands = commands.to_vec();
        Ok(self)
    }

    /// Endpoints an MCP `http` transport may name, matched like
    /// [`Self::provider_base_urls`].
    ///
    /// Raises:
    ///     AccessError: If an entry is not an `http` or `https` URL.
    pub fn mcp_urls(mut self, urls: &[String]) -> Result<Self, AccessError> {
        self.mcp_origins = origins("mcp_urls", urls)?;
        Ok(self)
    }
}

fn origins(list: &'static str, urls: &[String]) -> Result<Vec<Origin>, AccessError> {
    (urls.iter().enumerate())
        .map(|(index, url)| match Url::parse(url) {
            Ok(url) if matches!(url.scheme(), "http" | "https") => Ok(url.origin()),
            _ => Err(AccessError::Url { list, index }),
        })
        .collect()
}

/// Whether `url` shares an origin with one of `allowed`.
pub(super) fn allows(allowed: &[Origin], url: &str) -> bool {
    Url::parse(url).is_ok_and(|url| allowed.contains(&url.origin()))
}
