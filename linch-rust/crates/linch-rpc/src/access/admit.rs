//! The checks a tenant's requests pass before anything runs: no key is
//! read, no MCP process is started and no database is connected for a
//! refused request.

use std::io::ErrorKind;
use std::num::NonZeroU32;
use std::path::{Path, PathBuf};

use linch::permissions::RuleDecision;
use linch::verification::Check;
use linch::{
    AgentConfig, AgentSpec, ByteQuota, CacheScope, ExecutionConfig, McpTransportConfig,
    PermissionMode, PermissionPolicy, Principal, ProviderConfig, StoreConfig, SystemSection,
    ToolChoice,
};
use serde_json::json;

use super::allowlists::{McpCommand, PgTarget, allows};
use super::{Allowlists, Tenant};
use crate::error::{ErrorObject, code};

impl Tenant {
    /// Refuse `config` unless its namespace is this tenant's and its
    /// provider, endpoints, permissions, workspace, store and MCP servers
    /// are all allowed. The workspace, the project root and the config
    /// directory are replaced by the paths that were checked, `bash` is
    /// turned off unless the tenant may run exec tools, the execution
    /// backend must be one of the tenant's (absent: its first),
    /// `schedule_tools` is refused unless the tenant may hold schedules,
    /// `memory` unless it allows memory and caps it (its `max_memories` and
    /// `max_memories_per_scope` bounded by the tenant's cap), and
    /// a Postgres
    /// store that names no pool size gets the default size or the tenant's
    /// `max_pool_connections`, whichever is smaller.
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
        admit_guardrails(&mut config.spec, allow, &self.name)?;
        allow.admit_provider_concurrency(&mut config.max_provider_concurrency)?;
        admit_system(&mut config.spec, &allow.system_sections)?;
        let mode = (config.permissions.as_ref()).map_or(PermissionMode::default(), |p| p.mode);
        if !allow.permission_modes.contains(&mode) {
            // The config's spelling (`skip_dangerous`), not the variant's.
            let name = json!(mode);
            let name = name.as_str().unwrap_or_default();
            let message = format!("permission mode {name} is not in the tenant's permission_modes");
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
        allow.admit_execution(&mut config.execution)?;
        if config.schedule_tools && allow.max_schedules.is_none() {
            let message = "schedule_tools needs schedules in the tenant's allowlists \
                           (max_schedules)";
            return Err(refused("schedule_tools", message.into()));
        }
        if let Some(memory) = &mut config.memory {
            if !allow.memory {
                let message = "memory needs `memory` in the tenant's allowlists";
                return Err(refused("memory", message.into()));
            }
            // Memory without a cap is a table the model can fill: the
            // tenant names its budget, as it does for schedules.
            let Some(cap) = allow.max_memories else {
                let message = "memory needs a cap in the tenant's allowlists (max_memories)";
                return Err(refused("memory", message.into()));
            };
            // The smaller cap holds, and the tenant's applies when the config
            // names none; a stored definition meets a lowered cap the same way.
            let max = memory.max_memories.map_or(cap, |asked| asked.min(cap));
            memory.max_memories = Some(max);
            memory.max_memories_per_scope =
                (memory.max_memories_per_scope).map(|each| each.min(max));
        }
        if let StoreConfig::Postgres {
            dsn,
            max_connections,
            ..
        } = &mut config.stores
        {
            if !PgTarget::parse(dsn)
                .is_some_and(|target| allow.postgres.iter().any(|entry| entry.allows(&target)))
            {
                let message = "the store DSN does not name a server in the tenant's postgres_dsns";
                return Err(refused("postgres", message.into()));
            }
            let cap = allow.max_pool_connections;
            match *max_connections {
                Some(asked) if asked > cap => {
                    let message = format!(
                        "stores.max_connections {asked} is above the tenant's \
                         max_pool_connections {cap}"
                    );
                    let data = json!({"kind": "pool_connections", "max": cap});
                    return Err(ErrorObject::new(code::NOT_ALLOWED, message).with_data(data));
                }
                Some(_) => {}
                // The pool is the tenant's own, so the cap bounds it too.
                None => {
                    *max_connections = Some(linch::postgres::default_max_connections().min(cap));
                }
            }
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

    /// Refuse a run's own forced `tool_choice` that nothing would end (see
    /// the check `agent.open` runs on the spec's).
    pub(crate) fn admit_tool_choice(
        &self,
        choice: Option<&ToolChoice>,
        spec: &AgentSpec,
    ) -> Result<(), ErrorObject> {
        admit_tool_choice(choice, spec)
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
    /// A config that names a backend must name one of the tenant's
    /// `execution` entries exactly, so its policy (image, network, limits,
    /// paths) is the operator's; one that names none gets the first entry,
    /// which is how a tenant is held to a sandbox. The local backend stays
    /// absent, as in a config that never named it.
    fn admit_execution(&self, chosen: &mut Option<ExecutionConfig>) -> Result<(), ErrorObject> {
        let asked = chosen.clone().unwrap_or_default();
        let admitted = match chosen {
            Some(_) => self.execution.contains(&asked),
            None => match self.execution.first() {
                Some(first) => {
                    *chosen = (!first.is_local()).then(|| first.clone());
                    true
                }
                None => false,
            },
        };
        if !admitted {
            let message = format!(
                "execution backend {} is not in the tenant's execution list",
                asked.kind()
            );
            return Err(refused("execution", message));
        }
        if chosen.as_ref().is_some_and(ExecutionConfig::is_local) {
            *chosen = None;
        }
        Ok(())
    }

    /// A config may lower the provider concurrency to below the tenant's
    /// cap, not raise it past: one that asks for none gets the cap. The
    /// cap is not part of the run contract, so the fill changes no digest.
    fn admit_provider_concurrency(
        &self,
        asked: &mut Option<NonZeroU32>,
    ) -> Result<(), ErrorObject> {
        let Some(cap) = self.max_provider_concurrency else {
            return Ok(());
        };
        match *asked {
            Some(asked) if asked > cap => {
                let message = format!(
                    "max_provider_concurrency {asked} is above the tenant's \
                     max_provider_concurrency {cap}"
                );
                let data = json!({"kind": "provider_concurrency", "max": cap});
                Err(ErrorObject::new(code::NOT_ALLOWED, message).with_data(data))
            }
            Some(_) => Ok(()),
            None => {
                *asked = Some(cap);
                Ok(())
            }
        }
    }

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
        ProviderConfig::DeepSeek { base_url, .. }
        | ProviderConfig::OpenAiChat { base_url, .. }
        | ProviderConfig::OpenAiResponses { base_url, .. }
        | ProviderConfig::Anthropic { base_url, .. }
        | ProviderConfig::Gemini { base_url } => base_url.as_deref(),
        // Their keys are optional, but a key set in the environment is
        // still sent.
        ProviderConfig::Vllm { base_url, .. }
        | ProviderConfig::SgLang { base_url, .. }
        | ProviderConfig::LlamaCpp { base_url, .. } => Some(base_url),
        ProviderConfig::Scripted { .. } => None,
    }
}

/// Most redaction rules, verifiers, or keywords of one verifier, a tenant's
/// spec may hold: the server compiles and runs each on every response.
pub const MAX_GUARDRAILS: usize = 32;

/// Longest redaction or verifier pattern a tenant may give, in bytes; the
/// core bounds what one compiles to as well.
pub const MAX_PATTERN_BYTES: usize = 1024;

/// Most turns in a row a tenant's run may take without a provider call
/// (the loop guard's default `max_idle_turns`): each such turn is a write
/// and a session append on the server that no budget counts.
pub const MAX_IDLE_TURNS: u32 = 25;

/// Most attempts of one tool call a tenant's `tool_retry` may allow: each
/// retry runs the tool again and writes an event on the server.
pub const MAX_TOOL_ATTEMPTS: u32 = 5;

/// Longest wait between two attempts a tenant's `tool_retry` may allow: a
/// retrying call holds the run's place meanwhile.
pub const MAX_TOOL_RETRY_DELAY_MS: u64 = 60_000;

/// Most entries a tenant's `tool_cache` may keep for its namespace.
pub const MAX_TOOL_CACHE_ENTRIES: u32 = 1_024;

/// Longest a tenant's cached result may live, in milliseconds.
pub const MAX_TOOL_CACHE_TTL_MS: u64 = 3_600_000;

/// Largest result, as JSON bytes, a tenant's `tool_cache` may store.
pub const MAX_TOOL_CACHE_VALUE_BYTES: u64 = 1_048_576;

/// Refuse redaction rules and verifiers past [`MAX_GUARDRAILS`] or
/// [`MAX_PATTERN_BYTES`], a loop guard that allows more turns without
/// a provider call than [`MAX_IDLE_TURNS`], and a tool retry past
/// [`MAX_TOOL_ATTEMPTS`], [`MAX_TOOL_RETRY_DELAY_MS`] or [`MAX_GUARDRAILS`]
/// names, and a `tool_cache` the tenant may not use or past its bounds.
/// What the patterns match is the tenant's own business.
///
/// A `tool_cache` also needs `spec.redaction` over tool results (the cache
/// stores what the tool returned after that redaction, in the server's
/// memory or database), takes the session scope unless the tenant may share
/// entries across sessions, and has its `quota` set to the tenant's name
/// and the allowlist's byte cap, whatever the client sent: the cap bounds
/// every namespace of the tenant together.
fn admit_guardrails(
    spec: &mut AgentSpec,
    allow: &Allowlists,
    tenant: &str,
) -> Result<(), ErrorObject> {
    let redacts_results = spec.redaction.as_ref().is_some_and(|r| r.tool_results);
    if let Some(cache) = &mut spec.tool_cache {
        if !allow.tool_cache {
            let message = "tool_cache needs tool_cache in the tenant's allowlists";
            return Err(refused("tool_cache", message.into()));
        }
        if !redacts_results {
            let message = "tool_cache stores what a tool returned, so it needs spec.redaction \
                           with tool_results on";
            return Err(refused("tool_cache", message.into()));
        }
        if cache.scope == CacheScope::Namespace && !allow.tool_cache_shared {
            let message = "tool_cache scope namespace shares results across sessions and needs \
                           tool_cache_shared in the tenant's allowlists";
            return Err(refused("tool_cache", message.into()));
        }
        cache.quota = Some(ByteQuota {
            group: tenant.to_owned(),
            max_bytes: allow.tool_cache_bytes,
        });
        if cache.max_entries > MAX_TOOL_CACHE_ENTRIES
            || cache.ttl_ms > MAX_TOOL_CACHE_TTL_MS
            || cache.max_value_bytes > MAX_TOOL_CACHE_VALUE_BYTES
            || cache.tools.len() > MAX_GUARDRAILS
        {
            let message = format!(
                "tool_cache allows at most {MAX_TOOL_CACHE_ENTRIES} entries, a ttl of \
                 {MAX_TOOL_CACHE_TTL_MS} ms, {MAX_TOOL_CACHE_VALUE_BYTES} bytes a result and \
                 {MAX_GUARDRAILS} tools"
            );
            return Err(refused("tool_cache", message));
        }
    }
    if spec.loop_guard.max_idle_turns > MAX_IDLE_TURNS {
        let message = format!("loop_guard.max_idle_turns may be at most {MAX_IDLE_TURNS}");
        return Err(refused("loop_guard", message));
    }
    admit_tool_choice(spec.tool_choice.as_ref(), spec)?;
    if let Some(retry) = &spec.tool_retry {
        let names = |list: &[String]| list.len() > MAX_GUARDRAILS;
        if retry.max_attempts > MAX_TOOL_ATTEMPTS
            || retry.max_delay_ms > MAX_TOOL_RETRY_DELAY_MS
            || names(&retry.retry_on)
            || retry.tools.as_deref().is_some_and(names)
        {
            let message = format!(
                "tool_retry allows at most {MAX_TOOL_ATTEMPTS} attempts, a wait of \
                 {MAX_TOOL_RETRY_DELAY_MS} ms and {MAX_GUARDRAILS} names per list"
            );
            return Err(refused("tool_retry", message));
        }
    }
    let too_long = |pattern: &str| pattern.len() > MAX_PATTERN_BYTES;
    if let Some(redaction) = &spec.redaction
        && (redaction.rules.len() > MAX_GUARDRAILS
            || (redaction.rules.iter()).any(|rule| too_long(&rule.pattern)))
    {
        let message = format!(
            "redaction allows at most {MAX_GUARDRAILS} rules of at most \
             {MAX_PATTERN_BYTES} bytes each"
        );
        return Err(refused("redaction", message));
    }
    if let Some(verification) = &spec.verification {
        let oversized = |check: &Check| match check {
            Check::Matches { pattern } | Check::Forbids { pattern } => too_long(pattern),
            Check::Keywords { keywords, .. } => {
                keywords.len() > MAX_GUARDRAILS || keywords.iter().any(|k| too_long(k))
            }
        };
        if verification.verifiers.len() > MAX_GUARDRAILS
            || (verification.verifiers.iter()).any(|verifier| oversized(&verifier.check))
        {
            let message = format!(
                "verification allows at most {MAX_GUARDRAILS} verifiers, each with a \
                 pattern of at most {MAX_PATTERN_BYTES} bytes or at most {MAX_GUARDRAILS} \
                 keywords"
            );
            return Err(refused("verification", message));
        }
    }
    Ok(())
}

/// Refuse a forced tool choice (`required`, or a named tool that is not the
/// spec's final tool) for a run that nothing else ends: a model forced to
/// call a tool every turn never answers, so with no `max_turns` and no
/// `final_tool_name` the run goes on until a budget, which a tenant may not
/// have, or the loop guard, which an input that keeps changing evades. Only
/// the spec's named final tool counts, at `agent.open` and `run.start` alike:
/// the one made from an output schema exists only on a provider that cannot
/// take the schema natively, so a config that relies on it names the tool or
/// sets `max_turns`.
pub(crate) fn admit_tool_choice(
    choice: Option<&ToolChoice>,
    spec: &AgentSpec,
) -> Result<(), ErrorObject> {
    let final_tool = spec.final_tool_name.as_deref();
    let forced = match choice {
        Some(ToolChoice::Required {}) => true,
        Some(ToolChoice::Tool { name }) => Some(name.as_str()) != final_tool,
        _ => false,
    };
    if forced && spec.max_turns.is_none() && final_tool.is_none() {
        let message = "a forced tool_choice (required, or a tool that is not the final tool) \
                       needs spec.max_turns or spec.final_tool_name, or the run could never end";
        return Err(refused("tool_choice", message.into()));
    }
    Ok(())
}

fn refused(kind: &str, message: String) -> ErrorObject {
    ErrorObject::new(code::NOT_ALLOWED, message).with_data(json!({ "kind": kind }))
}

/// Most system prompt sections a tenant's spec may hold; the operator's
/// own (see `Allowlists::system_sections`) do not count.
pub const MAX_SYSTEM_SECTIONS: usize = 16;

/// Longest name of a system prompt section, in bytes.
pub const MAX_SECTION_NAME_BYTES: usize = 64;

/// Longest text of one system prompt section or of `system.append`, in
/// bytes.
pub const MAX_SECTION_BYTES: usize = 16 * 1024;

/// Most bytes the sections' texts and `system.append` of a tenant's spec
/// may hold together: they ride every request of every run.
pub const MAX_SYSTEM_BYTES: usize = 64 * 1024;

/// Longest hook policy id, and most ids in each list, of a context budget.
pub const MAX_BUDGET_POLICY_BYTES: usize = 128;

/// Replace the sections the operator sets, then bound the tenant's own:
/// at most [`MAX_SYSTEM_SECTIONS`] with names up to
/// [`MAX_SECTION_NAME_BYTES`], texts up to [`MAX_SECTION_BYTES`], and
/// [`MAX_SYSTEM_BYTES`] in all with `append`. A context budget names at
/// most [`MAX_GUARDRAILS`] hooks in each list, by ids up to
/// [`MAX_BUDGET_POLICY_BYTES`]. The operator's sections go first and win
/// a name the config holds, so admitting a config twice adds them once
/// and `replace_defaults` cannot remove them.
fn admit_system(spec: &mut AgentSpec, operator: &[SystemSection]) -> Result<(), ErrorObject> {
    let system = &mut spec.system;
    system
        .sections
        .retain(|own| !operator.iter().any(|set| set.name == own.name));
    let own = system.sections.len();
    let append = system.append.as_deref().map_or(0, str::len);
    let text: usize = system.sections.iter().map(|s| s.text.len()).sum();
    let too_big = own > MAX_SYSTEM_SECTIONS
        || (system.sections.iter())
            .any(|s| s.name.len() > MAX_SECTION_NAME_BYTES || s.text.len() > MAX_SECTION_BYTES)
        || append > MAX_SECTION_BYTES
        || text + append > MAX_SYSTEM_BYTES;
    if too_big {
        let message = format!(
            "system allows at most {MAX_SYSTEM_SECTIONS} sections of at most \
             {MAX_SECTION_BYTES} bytes and {MAX_SYSTEM_BYTES} bytes in all with append, \
             and section names of at most {MAX_SECTION_NAME_BYTES} bytes"
        );
        return Err(refused("system", message));
    }
    if let Some(budget) = &spec.context_budget {
        let ids = [&budget.drop_order, &budget.pinned];
        let oversized = ids.iter().any(|list| {
            list.len() > MAX_GUARDRAILS || list.iter().any(|id| id.len() > MAX_BUDGET_POLICY_BYTES)
        });
        if oversized {
            let message = format!(
                "context_budget names at most {MAX_GUARDRAILS} hooks in each list, by ids of at \
                 most {MAX_BUDGET_POLICY_BYTES} bytes"
            );
            return Err(refused("context_budget", message));
        }
    }
    let sections = &mut spec.system.sections;
    sections.splice(0..0, operator.iter().cloned());
    Ok(())
}
