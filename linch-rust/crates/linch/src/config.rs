//! [`AgentConfig`]: an agent as one strict serde value.

use std::fmt;
use std::path::PathBuf;

use linch_core::{AgentSpec, PermissionPolicy, ScriptedTurn, ToolSearch, Workers};
use serde::{Deserialize, Serialize};

use crate::profile::Profile;

mod deep_agent;
mod filesystem;
mod mcp;
mod redact;

pub use deep_agent::{DeepAgentConfig, DeepAgentProfileChoice};
pub use filesystem::FilesystemConfig;
pub use mcp::{McpConfig, McpTransportConfig, ReconnectConfig};

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
    /// `Grep`, `Glob`, and `Bash` unless `bash` is off). It must be an
    /// existing directory; a relative path resolves against the process's
    /// working directory. Absent: the agent has no tools.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace: Option<PathBuf>,
    /// Whether the workspace tools include `Bash`, which runs commands
    /// unconfined, as the process's user. On when absent; unused without a
    /// workspace.
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub bash: bool,
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
    /// `SubagentContinue` (see [`Workers`]). Off when absent; it needs
    /// `subagents`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workers: Option<Workers>,
    /// Whether the agent has an in-process mailbox and the `send_message`
    /// tool, for messages between its sessions (see
    /// [`linch_core::coordination`]). Off when absent.
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
    },
    /// OpenAI Chat Completions.
    #[serde(rename = "openai-chat")]
    OpenAiChat {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
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
    },
    /// The Anthropic Messages API.
    #[serde(rename = "anthropic")]
    Anthropic {
        /// API root; `ANTHROPIC_BASE_URL` or the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
    },
    /// Gemini `streamGenerateContent`.
    #[serde(rename = "gemini")]
    Gemini {
        /// API root; the public endpoint when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        base_url: Option<String>,
    },
    /// A vLLM OpenAI-compatible server.
    #[serde(rename = "vllm")]
    Vllm {
        /// API root including `/v1`.
        base_url: String,
    },
    /// An SGLang OpenAI-compatible server.
    #[serde(rename = "sglang")]
    SgLang {
        /// API root including `/v1`.
        base_url: String,
    },
    /// A llama.cpp server.
    #[serde(rename = "llamacpp")]
    LlamaCpp {
        /// API root including `/v1`.
        base_url: String,
    },
    /// Replays `turns` in call order, for tests and evals (see
    /// [`linch_core::ScriptedFixture`] for the turn shape).
    #[serde(rename = "scripted")]
    Scripted {
        /// One turn per provider call.
        turns: Vec<ScriptedTurn>,
    },
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
    },
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
            Self::Postgres { .. } => f
                .debug_struct("Postgres")
                .field("dsn", &"[redacted]")
                .finish(),
        }
    }
}
