//! [`ServerConfig`]: the listeners, the tenants with their bearer tokens,
//! and each tenant's allowlists, as one strict JSON value.

use std::collections::BTreeMap;
use std::fmt;
use std::net::SocketAddr;
use std::num::NonZeroU32;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use linch::{ExecutionConfig, PermissionMode, ReliabilityPolicy, SystemSection};
use linch_rpc::{AccessError, Allowlists, McpCommand, Tenant, Tokens, WorkflowCaps, new_server_id};
use serde::Deserialize;
use thiserror::Error;

/// What `linch-server --config FILE` serves.
///
/// Decoding rejects unknown fields at every level. `Debug` never shows a
/// token.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    /// Path of a Unix socket to listen on.
    #[serde(default)]
    pub socket: Option<PathBuf>,
    /// Address for HTTP with WebSocket, for example `127.0.0.1:8731`.
    #[serde(default)]
    pub http: Option<SocketAddr>,
    /// Every tenant by name.
    pub tenants: BTreeMap<String, TenantConfig>,
    /// Run a worker in this process: it fires the schedules and runs the
    /// queued jobs of the tenants' namespaces (and of `local_namespaces`)
    /// in one database. Absent: no worker, no scheduler loop, no query.
    #[serde(default)]
    pub worker: Option<WorkerSection>,
    /// How the HTTP listener notices a client that vanished without
    /// closing its connection (TCP keepalive, WebSocket pings). Every field
    /// has a default.
    #[serde(default)]
    pub liveness: crate::liveness::LivenessConfig,
    /// The name this server claims runs under: a refused resume (`-32033`)
    /// shows it as the holder's `owner`, and an operator reads which server
    /// drives the run from it. 1 to 64 ASCII letters, digits, `.`, `_` or
    /// `-`; 8 random hex characters when absent, printed at startup. Make it
    /// unique per server process, and not a host name or a pid, since a
    /// tenant sees it.
    #[serde(default)]
    pub server_id: Option<String>,
    /// The secret the tenants' prompt-cache salts are derived from, inline
    /// or from the environment, at least [`linch_rpc::MIN_SECRET_BYTES`]
    /// bytes. Required by a tenant whose `allow.cache_isolation` is on.
    /// Keep it the same on every server and worker of the fleet and across
    /// restarts: it is what makes one tenant's salt stable. Changing it
    /// gives every tenant a new salt, which starts their provider-side
    /// prompt caches cold.
    #[serde(default)]
    pub cache_salt_secret: Option<TokenSource>,
}

/// The `worker` section: where the worker's queue is and how it runs.
/// Each field but `dsn` takes [`linch_worker::WorkerConfig`]'s default
/// when absent.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerSection {
    /// The Postgres DSN of the database (and `search_path` schema) whose
    /// queue this worker serves, inline or from the environment: the one
    /// the tenants' agents store in. Background runs store through it, not
    /// through the DSN of the agent that scheduled them.
    pub dsn: TokenSource,
    /// The owner name of its job holds and run claims; unique per worker.
    /// The server's `server_id` when absent.
    #[serde(default)]
    pub owner: Option<String>,
    /// The queues it serves, one job from each in turn; `["default"]`.
    #[serde(default)]
    pub queues: Option<Vec<String>>,
    /// The most jobs it runs at once; 32.
    #[serde(default)]
    pub max_jobs: Option<usize>,
    /// The size of its own Postgres pool (the queue and the run records;
    /// each run's agent uses its tenant's pool); the default pool size.
    #[serde(default)]
    pub max_connections: Option<u32>,
    /// The longest wait between polls, and between two checks for due
    /// schedules; 1000.
    #[serde(default)]
    pub poll_interval_ms: Option<u64>,
    /// How long a taken job stays hidden without a heartbeat; 30000. Keep
    /// the agents' `stores.lease_ttl_ms` no longer than this.
    #[serde(default)]
    pub visibility_timeout_ms: Option<u64>,
    /// The wait before a job's second attempt, doubling each time; 5000.
    #[serde(default)]
    pub retry_delay_ms: Option<u64>,
    /// How long ended jobs stay in the history, and failed jobs kept;
    /// 7 days.
    #[serde(default)]
    pub history_retention_ms: Option<u64>,
    /// How long it waits for the jobs in flight after SIGTERM or SIGINT
    /// before it hands them back (their runs' leases and the jobs are
    /// released, so another worker takes them at once) and exits; 30000, at
    /// least 1. Keep the supervisor's termination grace period above it plus
    /// about 15 s.
    #[serde(default)]
    pub drain_timeout_ms: Option<u64>,
    /// Deleting what ended runs and idle sessions leave in the stores; off
    /// when absent. Every worker of the fleet may carry it: one pass runs at
    /// a time.
    #[serde(default)]
    pub retention: Option<RetentionSection>,
    /// Namespace roots outside every tenant whose jobs it also runs,
    /// without checks (those a stdio client scheduled): a root `dev`
    /// covers `dev` and `dev/...`.
    #[serde(default)]
    pub local_namespaces: Vec<String>,
}

/// The `worker.retention` section. At least one of `terminal_runs_days` and
/// `idle_sessions_days` is required; each is a window in whole days.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetentionSection {
    /// Delete a run (its record and events) that ended more than this many
    /// days ago and is terminal; never one that is running or suspended.
    #[serde(default)]
    pub terminal_runs_days: Option<u32>,
    /// Delete a session (its log and tasks) that nothing wrote to for this
    /// many days and that has no run that is not terminal.
    #[serde(default)]
    pub idle_sessions_days: Option<u32>,
    /// How many days a deleted run is remembered as pruned (then it reads as
    /// not found); 30.
    #[serde(default)]
    pub tombstone_days: Option<u32>,
    /// The most runs or sessions one transaction deletes; 500.
    #[serde(default)]
    pub batch_size: Option<u32>,
    /// The least seconds between two passes; 3600.
    #[serde(default)]
    pub interval_secs: Option<u64>,
}

/// One tenant: the tokens that name it and what its agents may use.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TenantConfig {
    /// Bearer tokens, each given inline or by environment variable.
    /// None for a tenant that only a worker serves.
    #[serde(default)]
    pub tokens: Vec<TokenSource>,
    /// The tenant's allowlists; every list empty when absent.
    #[serde(default)]
    pub allow: AllowConfig,
}

/// Where a bearer token comes from. `Debug` hides an inline value, and a
/// value in the wrong shape is refused without being repeated.
#[derive(Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "serde_json::Value")]
pub enum TokenSource {
    /// `{"env": "NAME"}`: the value of an environment variable of the
    /// server, read at startup.
    Env(String),
    /// `{"value": "..."}`: the token itself.
    Value(String),
}

impl TryFrom<serde_json::Value> for TokenSource {
    type Error = &'static str;

    /// Exactly one of `env` and `value`, a string. The error never holds
    /// the value, which may be the secret itself given in the wrong shape.
    fn try_from(value: serde_json::Value) -> Result<Self, Self::Error> {
        const SHAPE: &str = TOKEN_SHAPE;
        let serde_json::Value::Object(map) = value else {
            return Err(SHAPE);
        };
        let mut entries = map.into_iter();
        match (entries.next(), entries.next()) {
            (Some((key, serde_json::Value::String(text))), None) => match key.as_str() {
                "env" => Ok(Self::Env(text)),
                "value" => Ok(Self::Value(text)),
                _ => Err(SHAPE),
            },
            _ => Err(SHAPE),
        }
    }
}

/// What `TokenSource` says of a value that is not `{"env"}` or `{"value"}`.
const TOKEN_SHAPE: &str = "expected an object with `env` or `value`";

/// Kinds of value serde names, in its own words, at the start of an
/// `invalid type` or `invalid value` message.
const VALUE_KINDS: [&str; 15] = [
    "string",
    "integer",
    "floating point",
    "boolean",
    "character",
    "sequence",
    "map",
    "null",
    "unit value",
    "Option value",
    "newtype struct",
    "byte array",
    "enum",
    "unit variant",
    "struct variant",
];

/// The text of a config parse error without any string of the input. The
/// config holds secrets, and serde quotes the value that did not fit
/// (`invalid type: string "tok-..."`) and the key it did not know (`unknown
/// field `tok-...``), which `linch-server` would print on stderr.
///
/// The message is rebuilt from the parts that cannot come from the input:
/// its start (the kind of error, which serde writes before the input's
/// words) and its end (what was expected, which serde writes after them,
/// and which is the part after the last `, expected `). Whatever lies
/// between is dropped, whatever characters it holds. The line and column
/// come from the error, not its text. A message of another shape is
/// reduced to `invalid value`.
fn redact(error: &serde_json::Error) -> String {
    let full = error.to_string();
    let place = format!(" at line {} column {}", error.line(), error.column());
    let message = full.strip_suffix(&place).unwrap_or(&full);
    let place = if message.len() == full.len() {
        ""
    } else {
        place.as_str()
    };
    if error.classify() != serde_json::error::Category::Data {
        // A syntax error names a byte of the file, never a string of it.
        return format!("{message}{place}");
    }
    format!("{}{place}", data_error(message))
}

fn data_error(message: &str) -> String {
    const NO_FIELDS: &str = ", there are no fields";
    if message == TOKEN_SHAPE {
        return message.to_owned();
    }
    let expected = message.rfind(", expected ").map(|at| &message[at + 2..]);
    for kind in ["unknown field", "unknown variant"] {
        if message.starts_with(&format!("{kind} `")) {
            if message.ends_with(NO_FIELDS) {
                return format!("{kind}{NO_FIELDS}");
            }
            return match expected.filter(|expected| names_only(expected)) {
                Some(expected) => format!("{kind}, {expected}"),
                None => kind.to_owned(),
            };
        }
    }
    for lead in ["invalid type: ", "invalid value: "] {
        if let Some(rest) = message.strip_prefix(lead) {
            let kind = VALUE_KINDS.iter().find(|kind| rest.starts_with(**kind));
            let kind = kind.map_or(lead.trim_end_matches(": ").to_owned(), |kind| {
                format!("{lead}{kind}")
            });
            return match expected {
                Some(expected) => format!("{kind}, {expected}"),
                None => kind,
            };
        }
    }
    if let Some(name) = message.strip_prefix("missing field `")
        && name.strip_suffix('`').is_some_and(is_name)
    {
        return message.to_owned();
    }
    "invalid value".to_owned()
}

/// Whether `expected` is `expected` and then only field or variant names in
/// backticks, as serde lists them: `expected `a``, `expected `a` or `b``,
/// `expected one of `a`, `b``.
fn names_only(expected: &str) -> bool {
    let Some(list) = expected.strip_prefix("expected ") else {
        return false;
    };
    let mut rest = list.strip_prefix("one of ").unwrap_or(list);
    loop {
        let Some(open) = rest.strip_prefix('`') else {
            return false;
        };
        let Some((name, after)) = open.split_once('`') else {
            return false;
        };
        if !is_name(name) {
            return false;
        }
        if after.is_empty() {
            return true;
        }
        match after
            .strip_prefix(", ")
            .or_else(|| after.strip_prefix(" or "))
        {
            Some(next) => rest = next,
            None => return false,
        }
    }
}

fn is_name(text: &str) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

impl fmt::Debug for TokenSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Env(name) => f.debug_tuple("Env").field(name).finish(),
            Self::Value(_) => f.write_str("Value([redacted])"),
        }
    }
}

/// The `workflow` of an `allow` section: the caps on a tenant's
/// workflows. Every field is at least 1 when given.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct WorkflowAllow {
    /// Workflows the tenant drives at once, across its connections;
    /// [`linch_rpc::DEFAULT_MAX_WORKFLOWS`] when absent.
    pub max_workflows: Option<usize>,
    /// Journal records one workflow may hold;
    /// [`linch_rpc::DEFAULT_MAX_WORKFLOW_RECORDS`] when absent.
    pub max_records: Option<u64>,
    /// Agent calls one workflow may have in flight at once;
    /// [`linch_rpc::DEFAULT_MAX_WORKFLOW_FAN_OUT`] when absent.
    pub max_fan_out: Option<usize>,
    /// Bytes of one record's JSON value;
    /// [`linch_rpc::DEFAULT_MAX_WORKFLOW_RECORD_BYTES`] when absent.
    pub max_record_bytes: Option<usize>,
    /// The most tokens a workflow's budget may allow, which a workflow with
    /// no budget of its own gets; absent, no ceiling.
    pub max_tokens: Option<u64>,
    /// The most USD a workflow's budget may allow, as for `max_tokens`.
    pub max_cost_usd: Option<f64>,
}

/// The serde form of [`Allowlists`]; see its builder methods for how each
/// list matches.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AllowConfig {
    /// Whether the `scripted` provider is allowed (for tests and evals).
    pub scripted_provider: bool,
    /// Whether the tenant may ask a provider for its capabilities and its
    /// model list (`provider.capabilities`, `provider.models`,
    /// `provider.model`), which sends the server's key to the provider's
    /// `base_url` (that must be allowed too); off when absent.
    pub provider_info: bool,
    /// Whether a workspace agent gets `Bash`, which runs commands through
    /// its execution backend (unconfined, as the server's user, with
    /// `local`), with `BashOutput` and `BashKill`; off when absent.
    pub exec_tools: bool,
    /// Whether a config may set `spec.tool_cache`, which keeps tool results
    /// in the server's memory or database, and so only with
    /// `spec.redaction` over tool results; off when absent. The cache
    /// serves a result to the session that stored it unless
    /// `tool_cache_shared` allows `scope: "namespace"`, and every
    /// namespace of the tenant together holds at most
    /// `max_tool_cache_bytes`.
    pub tool_cache: bool,
    /// Whether a tenant's `tool_cache` may share results across the
    /// sessions of its namespace (`scope: "namespace"`); off when absent.
    pub tool_cache_shared: bool,
    /// Bytes of cached results all the tenant's namespaces may hold
    /// together, at least 1; [`linch_rpc::DEFAULT_TOOL_CACHE_BYTES`]
    /// (64 MiB) when absent.
    pub max_tool_cache_bytes: Option<u64>,
    /// Whether a config may set `task_ledger` (one task list shared by all
    /// the tenant's sessions, read on every request); off when absent.
    pub task_ledger: bool,
    /// Whether the tenant's agents send a salt derived from the tenant's
    /// name (HMAC under the config's `cache_salt_secret`) in the request
    /// field their provider has for it (`cache_salt` for `vllm`,
    /// `prompt_cache_key` for `openai-chat` and `openai-responses`), so a
    /// provider that caches prompt prefixes does not share them with
    /// another tenant on the same key. An agent on any other provider is
    /// refused (`kind` `cache_isolation`). Off when absent; needs
    /// `cache_salt_secret`.
    pub cache_isolation: bool,
    /// Whether a config's provider may set `request` (body fields,
    /// headers, a timeout) and `chat`; off when absent. Key, cookie, proxy
    /// and forwarding headers stay refused.
    pub provider_request: bool,
    /// Runs the tenant's connections may drive at once, together, at
    /// least 1; [`linch_rpc::DEFAULT_MAX_RUNS`] when absent.
    pub max_runs: Option<usize>,
    /// Connections that may act for the tenant at once, at least 1;
    /// [`linch_rpc::DEFAULT_MAX_CONNECTIONS`] when absent.
    pub max_connections: Option<usize>,
    /// Agents the tenant's connections may keep open at once, together,
    /// at least 1; [`linch_rpc::DEFAULT_MAX_AGENTS`] when absent.
    pub max_agents: Option<usize>,
    /// The largest `stores.max_connections` a config may ask for, and so
    /// the most connections of each of the tenant's own Postgres pools, at
    /// least 1; the server's default pool size
    /// ([`linch::postgres::default_max_connections`]) when absent.
    pub max_pool_connections: Option<u32>,
    /// The most provider calls the tenant's runs make at once on each
    /// provider key and endpoint, at least 1; absent, unlimited. A config
    /// may ask for less and gets this when it asks for nothing. It holds
    /// within the server process, not across processes.
    pub max_provider_concurrency: Option<u32>,
    /// Schedules the tenant may keep in each database, at least 1; absent,
    /// it may create none.
    pub max_schedules: Option<u64>,
    /// The shortest time between two fires of its schedules, in
    /// milliseconds; [`linch_rpc::DEFAULT_MIN_SCHEDULE_INTERVAL`] (60 s)
    /// when absent.
    pub min_schedule_interval_ms: Option<u64>,
    /// Background runs (`job.enqueue`) it may have unfinished at once in
    /// each database, at least 1; absent, it may queue none.
    pub max_queued_jobs: Option<u64>,
    /// Whether a config may turn on long-term `memory`; off when absent.
    /// It needs `max_memories`: the server refuses to load a config that
    /// allows memory without a cap.
    pub memory: bool,
    /// Memories the tenant may keep in each database, in all its
    /// namespaces, at least 1; required with `memory`.
    pub max_memories: Option<u64>,
    /// Whether the tenant may drive directed workflows (`workflow.*`),
    /// and what each may do; absent, none (every `workflow.start` is
    /// refused). `{}` allows them with the default caps.
    pub workflow: Option<WorkflowAllow>,
    /// Endpoints a provider `base_url` may name, matched on origin and
    /// path: an entry with a path allows that path alone, one with none
    /// allows each provider's own path (`ProviderConfig::default_base_paths`).
    pub provider_base_urls: Vec<String>,
    /// Permission modes a config may choose; `default` and `accept_edits`
    /// when absent. `auto_allow` and rules that allow need
    /// `skip_dangerous` here.
    pub permission_modes: Option<Vec<PermissionMode>>,
    /// Directories a workspace and a project root may be or lie under.
    pub workspace_roots: Vec<PathBuf>,
    /// Postgres servers, databases, users, options and TLS files a store
    /// DSN may use, and the weakest `sslmode` it may ask for.
    pub postgres_dsns: Vec<String>,
    /// MCP `stdio` servers a transport may start, each
    /// `{"command", "args"?, "env"?}`: the program and its arguments, and
    /// the names of the variables a transport may set.
    pub mcp_commands: Vec<McpCommand>,
    /// Endpoints an MCP `http` transport may name.
    pub mcp_urls: Vec<String>,
    /// Execution backends a config may choose, each with its whole policy
    /// (`{"backend": "sandbox", ...}`); a config naming none gets the
    /// first. `[{"backend": "local"}]` when absent; list only a sandbox to
    /// hold the tenant to it.
    pub execution: Option<Vec<ExecutionConfig>>,
    /// System prompt sections the operator adds to every agent of the
    /// tenant, each `{"name", "text", "order"?, "cacheable"?}`; a tenant
    /// cannot remove or rename them. None when absent.
    pub system_sections: Vec<SystemSection>,
    /// The reliability policy (`{"prerequisites", "gates",
    /// "max_corrections"}`, as a spec's `policy`) every agent of the
    /// tenant carries: its own `policy` may add rules, and the operator's
    /// stay. It orders the model's calls, counted from the agent's own
    /// tool results; it does not bind the tenant's host (a host tool, hook
    /// or cache can answer a required call), so it is not a security
    /// boundary. None when absent.
    pub required_policy: Option<ReliabilityPolicy>,
}

/// The server config could not be read or set up. No message holds a
/// token.
#[derive(Debug, Error)]
pub enum ConfigError {
    /// The file could not be read.
    #[error("cannot read the config: {0}")]
    Read(#[source] std::io::Error),
    /// The JSON is malformed or has an unknown or mistyped field. The text
    /// says where and what was expected, and never repeats a string of the
    /// input.
    #[error("invalid config: {0}")]
    Json(String),
    /// Neither `socket` nor `http` is set, and there is no `worker`.
    #[error("the config sets neither socket nor http nor worker")]
    NoListener,
    /// No tenant has a token, so every connection would be refused.
    #[error("the config gives no tenant a token")]
    NoToken,
    /// A tenant's `max_runs`, `max_connections`, `max_agents`,
    /// `max_pool_connections`, `max_schedules` or `max_queued_jobs` is 0.
    #[error(
        "tenant {tenant:?}: {field} is 0, which lets it do nothing; \
         set at least 1, or leave it out for the default"
    )]
    ZeroCap {
        /// The tenant.
        tenant: String,
        /// `max_runs`, `max_connections`, `max_agents` or
        /// `max_pool_connections`.
        field: &'static str,
    },
    /// A tenant allows `memory` without a `max_memories` cap, which would
    /// let a model fill its tables.
    #[error(
        "tenant {tenant:?}: allow.memory needs allow.max_memories, \
         a cap on the memories it may keep"
    )]
    MemoryNeedsCap {
        /// The tenant.
        tenant: String,
    },
    /// A tenant turns `allow.cache_isolation` on and the config has no
    /// `cache_salt_secret` to derive its salt from.
    #[error("tenant {tenant:?}: allow.cache_isolation needs the config's cache_salt_secret")]
    CacheIsolationNeedsSecret {
        /// The tenant.
        tenant: String,
    },
    /// The environment variable of `cache_salt_secret` is unset.
    #[error("environment variable {var} (cache_salt_secret) is unset")]
    CacheSecretEnv {
        /// The variable.
        var: String,
    },
    /// A `liveness` field is 0, which would end every connection at once
    /// or never check one.
    #[error("liveness.{field} is 0; set at least 1, or leave it out for the default")]
    ZeroLiveness {
        /// The field.
        field: &'static str,
    },
    /// `worker.drain_timeout_ms` is 0, which would hand back every job in
    /// flight the moment the server stops.
    #[error("worker.drain_timeout_ms is 0; set at least 1, or leave it out for the default")]
    ZeroDrainTimeout,
    /// `server_id` is empty, too long or has other characters.
    #[error("server_id must be 1-64 ASCII letters, digits, '.', '_' or '-'")]
    ServerId,
    /// A keepalive field is over what the kernel's TCP options take, which
    /// would fail on every connection.
    #[error("liveness.{field} is over {max}, the most the kernel takes")]
    OversizedLiveness {
        /// The field.
        field: &'static str,
        /// The largest value it takes.
        max: u64,
    },
    /// A token's environment variable is unset or not UTF-8.
    #[error("tenant {tenant:?}: environment variable {var} is unset")]
    TokenEnv {
        /// The tenant the token is for.
        tenant: String,
        /// The variable.
        var: String,
    },
    /// A tenant name, token or allowlist entry is invalid.
    #[error("tenant {tenant:?}: {source}")]
    Access {
        /// The tenant whose entry is invalid.
        tenant: String,
        /// What is wrong with it.
        #[source]
        source: AccessError,
    },
}

impl ServerConfig {
    /// Read a config from its JSON text.
    ///
    /// Raises:
    ///     ConfigError: If the JSON is invalid, sets no listener, gives
    ///         a tenant a cap of 0, or allows `memory` without
    ///         `max_memories`.
    pub fn from_json(text: &str) -> Result<Self, ConfigError> {
        let config: Self =
            serde_json::from_str(text).map_err(|error| ConfigError::Json(redact(&error)))?;
        if config.socket.is_none() && config.http.is_none() && config.worker.is_none() {
            return Err(ConfigError::NoListener);
        }
        if let Some(field) = config.liveness.zero_field() {
            return Err(ConfigError::ZeroLiveness { field });
        }
        if let Some((field, max)) = config.liveness.oversized_field() {
            return Err(ConfigError::OversizedLiveness { field, max });
        }
        if (config.worker.as_ref()).is_some_and(|worker| worker.drain_timeout_ms == Some(0)) {
            return Err(ConfigError::ZeroDrainTimeout);
        }
        if let Some(id) = &config.server_id {
            let valid = (1..=64).contains(&id.len())
                && id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b));
            if !valid {
                return Err(ConfigError::ServerId);
            }
        }
        for (name, tenant) in &config.tenants {
            let allow = &tenant.allow;
            let pool = allow.max_pool_connections.map(|max| max as usize);
            let caps = [
                ("max_runs", allow.max_runs),
                ("max_connections", allow.max_connections),
                ("max_agents", allow.max_agents),
                ("max_pool_connections", pool),
                (
                    "max_provider_concurrency",
                    allow.max_provider_concurrency.map(|max| max as usize),
                ),
                ("max_schedules", allow.max_schedules.map(|max| max as usize)),
                (
                    "max_queued_jobs",
                    allow.max_queued_jobs.map(|max| max as usize),
                ),
                (
                    "max_tool_cache_bytes",
                    allow.max_tool_cache_bytes.map(|max| max as usize),
                ),
                ("max_memories", allow.max_memories.map(|max| max as usize)),
                (
                    "workflow.max_workflows",
                    (allow.workflow.as_ref()).and_then(|w| w.max_workflows),
                ),
                (
                    "workflow.max_records",
                    (allow.workflow.as_ref()).and_then(|w| w.max_records.map(|max| max as usize)),
                ),
                (
                    "workflow.max_fan_out",
                    (allow.workflow.as_ref()).and_then(|w| w.max_fan_out),
                ),
                (
                    "workflow.max_record_bytes",
                    (allow.workflow.as_ref()).and_then(|w| w.max_record_bytes),
                ),
            ];
            if let Some((field, _)) = caps.into_iter().find(|(_, cap)| *cap == Some(0)) {
                let tenant = name.clone();
                return Err(ConfigError::ZeroCap { tenant, field });
            }
            if allow.memory && allow.max_memories.is_none() {
                let tenant = name.clone();
                return Err(ConfigError::MemoryNeedsCap { tenant });
            }
            if allow.cache_isolation && config.cache_salt_secret.is_none() {
                let tenant = name.clone();
                return Err(ConfigError::CacheIsolationNeedsSecret { tenant });
            }
        }
        Ok(config)
    }

    /// Build every tenant and its tokens, reading `{"env": ...}` tokens
    /// with `env`; [`Self::build`] without the tenants.
    ///
    /// Raises:
    ///     ConfigError: As [`Self::build`].
    pub fn tokens(&self, env: impl Fn(&str) -> Option<String>) -> Result<Tokens, ConfigError> {
        Ok(self.build(env)?.tokens)
    }

    /// Build every tenant and its tokens, reading `{"env": ...}` tokens
    /// with `env`. Blocks on the file system (workspace roots are
    /// canonicalized), so call it at startup.
    ///
    /// Args:
    ///     env: Looks up one environment variable, for example
    ///         `|name| std::env::var(name).ok()`.
    ///
    /// Returns:
    ///     The token table the listeners check every connection against,
    ///     and the tenants it names, which a worker shares.
    ///
    /// Raises:
    ///     ConfigError: If a variable is unset, a tenant, token or
    ///         allowlist entry is invalid, or the config has a listener
    ///         and no tenant has a token.
    pub fn build(&self, env: impl Fn(&str) -> Option<String>) -> Result<Built, ConfigError> {
        let mut tokens = Tokens::new();
        let mut tenants = Vec::new();
        let server_id = self.server_id.clone().unwrap_or_else(new_server_id);
        let secret = match &self.cache_salt_secret {
            None => None,
            Some(TokenSource::Value(secret)) => Some(secret.clone()),
            Some(TokenSource::Env(var)) => {
                Some(env(var).ok_or_else(|| ConfigError::CacheSecretEnv { var: var.clone() })?)
            }
        };
        for (name, tenant) in &self.tenants {
            let access = |source| ConfigError::Access {
                tenant: name.clone(),
                source,
            };
            let mut made =
                Tenant::new(name, tenant.allow.build().map_err(access)?).map_err(access)?;
            if tenant.allow.cache_isolation {
                let Some(secret) = &secret else {
                    let tenant = name.clone();
                    return Err(ConfigError::CacheIsolationNeedsSecret { tenant });
                };
                made = made
                    .with_cache_isolation(secret.as_bytes())
                    .map_err(access)?;
            }
            let built = Arc::new(made.with_server_id(&server_id));
            for source in &tenant.tokens {
                let token = match source {
                    TokenSource::Value(token) => token.clone(),
                    TokenSource::Env(var) => env(var).ok_or_else(|| ConfigError::TokenEnv {
                        tenant: name.clone(),
                        var: var.clone(),
                    })?,
                };
                tokens.insert(&token, Arc::clone(&built)).map_err(access)?;
            }
            tenants.push(built);
        }
        let listens = self.socket.is_some() || self.http.is_some();
        if listens && tokens.is_empty() {
            return Err(ConfigError::NoToken);
        }
        Ok(Built {
            tokens,
            tenants,
            server_id,
        })
    }
}

/// What [`ServerConfig::build`] makes: the token table and the tenants.
#[derive(Debug)]
pub struct Built {
    /// The tokens the listeners check.
    pub tokens: Tokens,
    /// Every tenant, each shared by its tokens.
    pub tenants: Vec<Arc<Tenant>>,
    /// The name this server claims runs under: the config's `server_id`,
    /// or a random one.
    pub server_id: String,
}

impl WorkflowAllow {
    fn caps(&self) -> WorkflowCaps {
        let defaults = WorkflowCaps::default();
        WorkflowCaps {
            max_workflows: self.max_workflows.unwrap_or(defaults.max_workflows),
            max_records: self.max_records.unwrap_or(defaults.max_records),
            max_fan_out: self.max_fan_out.unwrap_or(defaults.max_fan_out),
            max_record_bytes: self.max_record_bytes.unwrap_or(defaults.max_record_bytes),
            max_tokens: self.max_tokens,
            max_cost_usd: self.max_cost_usd,
        }
    }
}

impl AllowConfig {
    /// The [`Allowlists`] this describes.
    ///
    /// Raises:
    ///     AccessError: If a URL or DSN is invalid, a root cannot be
    ///         resolved, an MCP entry lists a refused variable, or the
    ///         required policy has a check that cannot be used.
    pub fn build(&self) -> Result<Allowlists, AccessError> {
        let mut allow = Allowlists::new()
            .scripted_provider(self.scripted_provider)
            .provider_info(self.provider_info)
            .exec_tools(self.exec_tools)
            .tool_cache(self.tool_cache)
            .tool_cache_shared(self.tool_cache_shared)
            .tool_cache_bytes(
                (self.max_tool_cache_bytes).unwrap_or(linch_rpc::DEFAULT_TOOL_CACHE_BYTES),
            )
            .task_ledger(self.task_ledger)
            .provider_request(self.provider_request)
            .max_runs(self.max_runs.unwrap_or(linch_rpc::DEFAULT_MAX_RUNS))
            .max_connections((self.max_connections).unwrap_or(linch_rpc::DEFAULT_MAX_CONNECTIONS))
            .max_agents(self.max_agents.unwrap_or(linch_rpc::DEFAULT_MAX_AGENTS))
            .max_pool_connections(
                (self.max_pool_connections.and_then(NonZeroU32::new))
                    .unwrap_or_else(linch::postgres::default_max_connections),
            )
            .max_provider_concurrency(self.max_provider_concurrency.and_then(NonZeroU32::new))
            .provider_base_urls(&self.provider_base_urls)?
            .workspace_roots(&self.workspace_roots)?
            .postgres_dsns(&self.postgres_dsns)?
            .mcp_commands(&self.mcp_commands)?
            .mcp_urls(&self.mcp_urls)?
            .max_schedules(self.max_schedules)
            .max_queued_jobs(self.max_queued_jobs)
            .system_sections(&self.system_sections)?
            .memory(self.memory)
            .max_memories(self.max_memories)
            .workflow(self.workflow.as_ref().map(WorkflowAllow::caps));
        if let Some(ms) = self.min_schedule_interval_ms {
            allow = allow.min_schedule_interval(Duration::from_millis(ms));
        }
        if let Some(modes) = &self.permission_modes {
            allow = allow.permission_modes(modes);
        }
        if let Some(backends) = &self.execution {
            allow = allow.execution(backends);
        }
        if let Some(policy) = &self.required_policy {
            if let Some(reason) = policy.compile_problem() {
                return Err(AccessError::RequiredPolicy { reason });
            }
            allow = allow.required_policy(Some(policy.clone()));
        }
        Ok(allow)
    }
}

#[cfg(test)]
mod tests;
