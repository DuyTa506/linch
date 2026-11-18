//! [`AgentConfig`]: an agent as one strict serde value.

use std::fmt;
use std::num::NonZeroU32;
use std::path::PathBuf;

use linch_core::{AgentSpec, PermissionPolicy, ScriptedTurn, ToolSearch, Workers};
pub use linch_tools::{
    DockerPolicy, ExecutionConfig, RemoteChunk, RemoteCommand, RemoteError, RemoteExec,
    RemotePolicy, SandboxPolicy,
};
use serde::{Deserialize, Deserializer, Serialize};

use crate::profile::Profile;

mod deep_agent;
mod filesystem;
mod mcp;
mod memory;
mod pricing;
mod redact;
mod request;

pub use deep_agent::{DeepAgentConfig, DeepAgentProfileChoice};
pub use filesystem::FilesystemConfig;
pub use mcp::{McpConfig, McpTransportConfig, ReconnectConfig};
pub use memory::{MemoryConfig, RecallConfig};
pub(crate) use pricing::pricing;
pub use pricing::{ModelRates, PricingOverrides};
pub use request::{ChatConfig, RequestConfig};

/// A task list shared by every session of an agent, shown with each request
/// ([`AgentConfig::task_ledger`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskLedger {
    /// The list's id. It is stored as an empty session of the agent's
    /// namespace, so pick one no run uses as its session id.
    pub list: String,
    /// Most characters of ledger text a request carries
    /// ([`linch_tools::DEFAULT_LEDGER_CHARS`] when absent, at least 200); the
    /// open tasks past it are named as a count. The block's policy id,
    /// [`linch_tools::LEDGER_POLICY_ID`], can be pinned in
    /// `spec.context_budget` so a budget never drops it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_chars: Option<usize>,
}

/// Everything needed to open an [`Agent`](linch_core::Agent), as data.
///
/// Decoding rejects unknown fields at every level, so a misspelt or
/// unsupported option (an `api_key`, say) fails instead of being ignored.
/// Open it with [`AgentConfig::open`].
///
/// Serializing is for logs and echoes, so it hides the secrets: the DSN
/// password and the MCP env and header values. Send or store a config
/// with [`AgentConfig::to_value_with_secrets`], which keeps them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentConfig {
    /// Store key space for this agent's runs and sessions.
    pub namespace: String,
    /// The model, instructions and loop limits.
    pub spec: AgentSpec,
    /// The model provider and its options.
    pub provider: ProviderConfig,
    /// Where runs and sessions are stored; memory when absent.
    #[serde(default)]
    pub stores: StoreConfig,
    /// Workspace root for the built-in tools (`Read`, `Write`, `Edit`,
    /// `Grep`, `Glob`, and `Bash`, `BashOutput` and `BashKill` unless
    /// `bash` is off). It must be an existing directory; a relative path
    /// resolves against the process's working directory. Absent: the agent
    /// has no tools.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace: Option<PathBuf>,
    /// Whether the workspace tools include `Bash` (which runs commands
    /// through `execution`) and `BashOutput` and `BashKill`, which read and
    /// stop its background commands. On when absent; unused without a
    /// workspace.
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub bash: bool,
    /// Where `Bash` runs its commands (see [`ExecutionConfig`]): `local`,
    /// directly as the process's user, when absent; `sandbox` (Landlock,
    /// seccomp and rlimits), `docker`, or `remote` (a host the agent's
    /// client supplies, which only a host that serves it can open: see
    /// [`AgentConfig::open_with_remote`]). A non-local backend is part of
    /// the run contract, through `Bash`'s description. Unused without
    /// `bash`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution: Option<ExecutionConfig>,
    /// Permission rules. Absent: mode `default` (read tools run, write and
    /// exec tools ask) with the workspace root, or `/`, as project root.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub permissions: Option<PermissionPolicy>,
    /// MCP servers, connected in order when the agent opens. Their tools
    /// at that moment enter the run contract; a tool a server adds later
    /// does not.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub mcp_servers: Vec<McpConfig>,
    /// Whether the agent has skills: the built-in `verify` and those in
    /// `config_dir/skills` (see [`crate::project::load_skills`]). Off when
    /// absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub skills: bool,
    /// Whether the agent has foreground subagents: the built-in
    /// `verification` and those in `config_dir/agents` (see
    /// [`crate::project::load_subagents`]). Off when absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub subagents: bool,
    /// How subagents keep their workers: in the background, or kept for
    /// `SubagentContinue`, and how many background workers one run may
    /// have running (`max_background`, 4 unless set; see [`Workers`]). Off
    /// when absent; it needs `subagents`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workers: Option<Workers>,
    /// Whether the agent has a mailbox and the `send_message` tool, for
    /// messages between its sessions (see [`linch_core::coordination`]):
    /// a [`linch_postgres::PgMailbox`] of the agent's namespace with
    /// Postgres stores, an in-process one with memory stores. Off when
    /// absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub mailbox: bool,
    /// Past which sizes part of the catalog waits for the model to find it
    /// with `tool_search` (see [`linch_core::tool_search`]). Off when
    /// absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_search: Option<ToolSearch>,
    /// Directory holding `skills/` and `agents/`. Absent: `.linch`. A
    /// relative path resolves against the workspace root, or the process's
    /// working directory without a workspace. Unused unless `skills` or
    /// `subagents` is on.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub config_dir: Option<PathBuf>,
    /// The virtual filesystem tools and large-result offload (see
    /// [`FilesystemConfig`]). Off when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub filesystem: Option<FilesystemConfig>,
    /// Whether the agent has the session task tools (`TaskCreate`,
    /// `TaskList`, `TaskGet`, `TaskUpdate`), kept in the session store. As
    /// in Python they run without approval in every mode: the policy's
    /// `auto_allow` gets their names. Off when absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub tasks: bool,
    /// Turns the task tools into a ledger that outlives sessions, and shows
    /// it to the model: every session of the agent shares one task list
    /// ([`TaskLedger::list`]) instead of keeping its own, and each request
    /// carries the open tasks as extra context (never logged). Needs
    /// `tasks`. Off when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_ledger: Option<TaskLedger>,
    /// Whether the agent has the `AskUser` tool (see
    /// [`linch_core::ask_user`]): a call suspends the run with an
    /// `input_request` until the host answers it. Scope `read`, so it runs
    /// unasked in modes that allow reads. Off when absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub ask_user: bool,
    /// Whether the agent has the schedule tools (`CreateSchedule`,
    /// `ListSchedules`, `CancelSchedule`): the model schedules runs of
    /// this same agent in its own session. The facade does not bind them:
    /// a host that runs the schedules (linch-server, with a worker) puts
    /// them in the registry first, and the open refuses the switch when
    /// they are missing or the stores are not Postgres. Off when absent.
    #[serde(default, skip_serializing_if = "is_false")]
    pub schedule_tools: bool,
    /// Long-term memory: the `MemorySave`, `MemorySearch` and `MemoryDelete`
    /// tools, and optionally a search before each request (see
    /// [`MemoryConfig`]). It needs no host callback, so scheduled and
    /// background agents have it. Off when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory: Option<MemoryConfig>,
    /// The deep agent preset: planning, a subagent roster, kept workers and
    /// file memory (see [`DeepAgentConfig`] for what it turns on). Off
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deep_agent: Option<DeepAgentConfig>,
    /// The domain profile the agent's runs take (see [`Profile`]): its
    /// instructions after the system prompt (and the deep agent's), its
    /// `max_turns`, and a permission layer denying its `tool_deny`. None
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub profile: Option<Profile>,
    /// Rates, by exact model id, that this agent prices a model with
    /// instead of the published table, or in addition to it: a local model
    /// the table does not list has no cost until priced here (see
    /// [`ModelRates`]). Empty when absent.
    #[serde(
        default,
        deserialize_with = "pricing::check",
        skip_serializing_if = "PricingOverrides::is_empty"
    )]
    pub pricing: PricingOverrides,
    /// The most provider calls this agent's runs, and the runs of every
    /// agent opened with the same [`PgPools`](crate::PgPools) on the same
    /// provider key and endpoint, make at once (Python's
    /// `max_provider_concurrency`); the rest wait their turn, first come
    /// first served. A run that waits has dispatched nothing, so cancelling
    /// it leaves no delivery or receipt. Unlimited when absent, which keeps
    /// it off the wire; it is not part of the run contract, so changing it
    /// never strands a suspended run. A cap of one process: runs in another
    /// process (a worker, a second server) count separately.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_provider_concurrency: Option<NonZeroU32>,
    /// What the agent reports about its own runs beyond the events every run
    /// has (see [`MonitoringConfig`]). Off when absent, which keeps it off
    /// the wire; it is not part of the run contract.
    #[serde(default, skip_serializing_if = "MonitoringConfig::is_default")]
    pub monitoring: MonitoringConfig,
}

/// The `monitoring` section of an [`AgentConfig`]: switches for what an
/// agent reports about its runs. Every switch is off when absent, and an
/// agent that sets none sends, stores and emits exactly what it did without
/// the section. None is part of the run contract.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MonitoringConfig {
    /// Emit a stream-only `cache_diagnostic` event after each provider call
    /// that reports its input tokens, against the run's previous call: why
    /// the provider's prompt cache read what it did (the `CacheDiagnosticEvent`
    /// of `linch_core`). It keeps a hash
    /// chain of each request, so a long conversation costs one pass over it
    /// per call.
    #[serde(default, skip_serializing_if = "is_false")]
    pub cache_diagnostic: bool,
}

impl MonitoringConfig {
    /// Whether every switch is off.
    pub fn is_default(&self) -> bool {
        self == &Self::default()
    }
}

impl AgentConfig {
    /// Whether this agent's `Bash` runs its commands on a remote host: a
    /// `remote` backend, with `Bash` registered (a `workspace`, and `bash`
    /// on). Only such an agent needs a live client connection; one without
    /// `Bash` never starts a command, whatever backend its config names.
    pub fn runs_bash_remotely(&self) -> bool {
        let remote = matches!(self.execution, Some(ExecutionConfig::Remote(_)));
        remote && self.bash && self.workspace.is_some()
    }
}

fn is_false(value: &bool) -> bool {
    !value
}

fn is_true(value: &bool) -> bool {
    *value
}

/// The model provider, chosen by `id`.
///
/// Each live provider reads its key from the process environment when the
/// agent opens; a config never carries a key. `base_url` overrides the
/// provider's public endpoint.
///
/// | `id` | Key variable | Default endpoint |
/// |---|---|---|
/// | `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` |
/// | `openai-chat` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
/// | `openai-responses` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
/// | `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL`, else `https://api.anthropic.com` |
/// | `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com` |
/// | `vllm` | `VLLM_API_KEY`, optional | none, `base_url` required |
/// | `sglang` | `SGLANG_API_KEY`, optional | none, `base_url` required |
/// | `llamacpp` | `LLAMA_API_KEY`, optional | none, `base_url` required |
/// | `scripted` | none | none: replays `turns` |
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "id", deny_unknown_fields)]
pub enum ProviderConfig {
    /// DeepSeek's Chat Completions API.
    #[serde(rename = "deepseek")]
    DeepSeek {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
        /// The context window of every model; absent: 128000.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
        /// Chat Completions settings.
        #[serde(default, skip_serializing_if = "ChatConfig::is_default")]
        chat: ChatConfig,
    },
    /// OpenAI Chat Completions.
    #[serde(rename = "openai-chat")]
    OpenAiChat {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
        /// The context window of every model; absent: Python's table (gpt-5.5 and gpt-5.4 400000, o1
        /// 200000, others 128000).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
        /// Chat Completions settings.
        #[serde(default, skip_serializing_if = "ChatConfig::is_default")]
        chat: ChatConfig,
    },
    /// The OpenAI Responses API.
    #[serde(rename = "openai-responses")]
    OpenAiResponses {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
        /// Reasoning summary to stream as thinking; none when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reasoning_summary: Option<ReasoningSummaryConfig>,
        /// Whether OpenAI stores each response (default `true`).
        #[serde(default = "yes")]
        store: bool,
        /// Whether a tool added or removed mid-session goes in the input as
        /// `additional_tools`, keeping the prompt cache (gpt-5.4 and later;
        /// off by default, not verified against the live API).
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        native_catalog_changes: bool,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
    },
    /// The Anthropic Messages API.
    #[serde(rename = "anthropic")]
    Anthropic {
        /// API root; `ANTHROPIC_BASE_URL` or the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
        /// Whether a tool added or removed mid-session goes in the
        /// conversation as a tool block, keeping the prompt cache (beta
        /// `inline-tools-2026-09-15`, on `api.anthropic.com` with the models
        /// that take it; off by default, not verified against the live API).
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        native_catalog_changes: bool,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
    },
    /// Gemini `streamGenerateContent`.
    #[serde(rename = "gemini")]
    Gemini {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
    },
    /// A vLLM OpenAI-compatible server.
    #[serde(rename = "vllm")]
    Vllm {
        /// API root including `/v1`.
        base_url: String,
        /// The context window of every model; absent: the server is asked when the agent opens
        /// (`max_model_len` on `/v1/models`), else 128000.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
        /// Chat Completions settings.
        #[serde(default, skip_serializing_if = "ChatConfig::is_default")]
        chat: ChatConfig,
    },
    /// An SGLang OpenAI-compatible server.
    #[serde(rename = "sglang")]
    SgLang {
        /// API root including `/v1`.
        base_url: String,
        /// The context window of every model; absent: the server is asked when the agent opens
        /// (`max_model_len` on `/v1/models`), else 128000.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
        /// Chat Completions settings.
        #[serde(default, skip_serializing_if = "ChatConfig::is_default")]
        chat: ChatConfig,
    },
    /// A llama.cpp server.
    #[serde(rename = "llamacpp")]
    LlamaCpp {
        /// API root including `/v1`.
        base_url: String,
        /// The context window of every model; absent: the server is asked when the agent opens
        /// (`n_ctx` on `/props`), else 128000.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Extra body fields, headers and a timeout for every request.
        #[serde(default, skip_serializing_if = "RequestConfig::is_default")]
        request: RequestConfig,
        /// Chat Completions settings.
        #[serde(default, skip_serializing_if = "ChatConfig::is_default")]
        chat: ChatConfig,
    },
    /// Replays `turns` in call order, for tests and evals (see
    /// [`linch_core::ScriptedFixture`] for the turn shape).
    #[serde(rename = "scripted")]
    Scripted {
        /// One turn per provider call.
        turns: Vec<ScriptedTurn>,
        /// The context window of every model; absent: 128000.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        context_window: Option<u64>,
        /// Whether it takes an output schema natively; false makes a run
        /// with a schema offer its final tool, as DeepSeek or a local
        /// server does. True when absent.
        #[serde(default = "native", skip_serializing_if = "is_native")]
        native_output_schema: bool,
    },
}

impl ProviderConfig {
    /// The paths a `base_url` may take on an origin an operator allowed
    /// without naming a path: where this provider's own API root sits
    /// (`/v1` for OpenAI's Chat Completions and Responses, none for
    /// DeepSeek, Anthropic and Gemini, whose adapters add the version
    /// themselves, `/v1` or none for a self-hosted server). A trailing
    /// slash does not matter. A host that serves the API under another
    /// prefix lists that prefix in full.
    pub fn default_base_paths(&self) -> &'static [&'static str] {
        match self {
            Self::OpenAiChat { .. } | Self::OpenAiResponses { .. } => &["/v1"],
            Self::Vllm { .. } | Self::SgLang { .. } | Self::LlamaCpp { .. } => &["/v1", ""],
            Self::DeepSeek { .. } | Self::Anthropic { .. } | Self::Gemini { .. } => &[""],
            Self::Scripted { .. } => &[],
        }
    }

    /// The request settings of a live provider; `None` for `scripted`.
    pub fn request(&self) -> Option<&RequestConfig> {
        match self {
            Self::DeepSeek { request, .. }
            | Self::OpenAiChat { request, .. }
            | Self::OpenAiResponses { request, .. }
            | Self::Anthropic { request, .. }
            | Self::Gemini { request, .. }
            | Self::Vllm { request, .. }
            | Self::SgLang { request, .. }
            | Self::LlamaCpp { request, .. } => Some(request),
            Self::Scripted { .. } => None,
        }
    }

    /// The request settings of a live provider, to change; `None` for
    /// `scripted`.
    pub fn request_mut(&mut self) -> Option<&mut RequestConfig> {
        match self {
            Self::DeepSeek { request, .. }
            | Self::OpenAiChat { request, .. }
            | Self::OpenAiResponses { request, .. }
            | Self::Anthropic { request, .. }
            | Self::Gemini { request, .. }
            | Self::Vllm { request, .. }
            | Self::SgLang { request, .. }
            | Self::LlamaCpp { request, .. } => Some(request),
            Self::Scripted { .. } => None,
        }
    }

    /// The body field this provider takes a [`RequestConfig::cache_salt`]
    /// in, or `None` when its vendor has no field for one: `cache_salt` for
    /// `vllm`, `prompt_cache_key` for `openai-chat` and `openai-responses`.
    /// `deepseek`, `anthropic`, `gemini`, `sglang` and `llamacpp` have none,
    /// and `scripted` makes no request.
    pub fn cache_isolation_field(&self) -> Option<&'static str> {
        match self {
            Self::Vllm { .. } => Some("cache_salt"),
            Self::OpenAiChat { .. } | Self::OpenAiResponses { .. } => Some("prompt_cache_key"),
            Self::DeepSeek { .. }
            | Self::Anthropic { .. }
            | Self::Gemini { .. }
            | Self::SgLang { .. }
            | Self::LlamaCpp { .. }
            | Self::Scripted { .. } => None,
        }
    }

    /// The Chat Completions settings of `deepseek`, `openai-chat`, `vllm`,
    /// `sglang` and `llamacpp`; `None` for the others.
    pub fn chat(&self) -> Option<&ChatConfig> {
        match self {
            Self::DeepSeek { chat, .. }
            | Self::OpenAiChat { chat, .. }
            | Self::Vllm { chat, .. }
            | Self::SgLang { chat, .. }
            | Self::LlamaCpp { chat, .. } => Some(chat),
            Self::OpenAiResponses { .. }
            | Self::Anthropic { .. }
            | Self::Gemini { .. }
            | Self::Scripted { .. } => None,
        }
    }
}

fn native() -> bool {
    true
}

fn is_native(value: &bool) -> bool {
    *value
}

fn yes() -> bool {
    true
}

/// How much reasoning the Responses API summarizes back as thinking.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningSummaryConfig {
    /// The most detailed summary the model supports.
    Auto,
    /// A short summary.
    Concise,
    /// A detailed summary.
    Detailed,
}

/// Where an agent's runs and sessions live.
///
/// `Debug` hides the DSN, which usually holds a password; serializing
/// writes it with the password replaced (see
/// [`AgentConfig::to_value_with_secrets`]).
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum StoreConfig {
    /// In-process memory stores owned by the opened agent, for tests and
    /// development. They are dropped with the agent.
    Memory {},
    /// Postgres stores. Opening takes the DSN's pool from the
    /// [`PgPools`](crate::PgPools) it is given; when no open agent uses
    /// one, it connects, giving up after
    /// [`linch_postgres::CONNECT_TIMEOUT`] when the server cannot be
    /// reached, and applies the migrations, giving up after 30 s.
    Postgres {
        /// Connection string, for example `postgres://user:pass@host/db`.
        #[serde(serialize_with = "redact::dsn")]
        dsn: String,
        /// The most connections the pool opens at once;
        /// [`linch_postgres::default_max_connections`] when absent (the
        /// machine's cores, at least 10 and at most 32). Only the
        /// open that connects the DSN's pool sets it: agents that share
        /// that pool share its size.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_connections: Option<NonZeroU32>,
        /// How long a run claim of this agent holds without a renewal, in
        /// milliseconds, at least [`MIN_LEASE_TTL_MS`];
        /// [`linch_postgres::DEFAULT_RUN_LEASE_TTL`] (30 s) when absent.
        /// While a run is driven its lease is renewed every third of it;
        /// another process's resume of the run is refused until the lease
        /// is released or runs out, so this is the longest a run waits
        /// after its holder crashed.
        #[serde(
            default,
            skip_serializing_if = "Option::is_none",
            deserialize_with = "lease_ttl_ms"
        )]
        lease_ttl_ms: Option<u64>,
    },
}

/// The shortest `stores.lease_ttl_ms`: renewals every third of it, so at
/// most three small writes a second per running run.
pub const MIN_LEASE_TTL_MS: u64 = 1000;

fn lease_ttl_ms<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<u64>, D::Error> {
    let ttl = u64::deserialize(deserializer)?;
    if ttl < MIN_LEASE_TTL_MS {
        let message = format!("lease_ttl_ms {ttl} is below the minimum {MIN_LEASE_TTL_MS}");
        return Err(serde::de::Error::custom(message));
    }
    Ok(Some(ttl))
}

impl Default for StoreConfig {
    fn default() -> Self {
        Self::Memory {}
    }
}

impl fmt::Debug for StoreConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Memory {} => f.write_str("Memory"),
            Self::Postgres {
                max_connections,
                lease_ttl_ms,
                ..
            } => f
                .debug_struct("Postgres")
                .field("dsn", &"[redacted]")
                .field("max_connections", max_connections)
                .field("lease_ttl_ms", lease_ttl_ms)
                .finish(),
        }
    }
}
