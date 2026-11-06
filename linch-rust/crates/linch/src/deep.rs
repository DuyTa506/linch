//! What [`AgentConfig::deep_agent`] adds when an agent opens (see
//! [`crate::DeepAgentConfig`]).

use linch_ext::deep_agent::{DeepAgent, DeepAgentError};

use crate::config::{AgentConfig, FilesystemConfig, StoreConfig};
use crate::error::OpenError;
use crate::project::Loaded;

/// Resolve the preset of `config` and turn on what it needs in the config
/// itself: the task tools, the virtual filesystem and the profile's
/// workers (unless set), and the preset's spec. `None` when the config has
/// no preset. Nothing is opened or registered here.
///
/// Raises:
///     OpenError: `DeepAgent` for an unknown profile, or a durable deep
///         agent on memory stores.
pub(crate) fn expand(config: &mut AgentConfig) -> Result<Option<DeepAgent>, OpenError> {
    let Some(preset) = &config.deep_agent else {
        return Ok(None);
    };
    let deep = preset.resolve()?;
    if deep.durable && !matches!(config.stores, StoreConfig::Postgres { .. }) {
        return Err(DeepAgentError::NotDurable.into());
    }
    config.tasks = true;
    config
        .filesystem
        .get_or_insert_with(FilesystemConfig::default);
    config.workers = config.workers.or(Some(deep.workers()));
    config.spec = deep.spec(config.spec.clone());
    Ok(Some(deep))
}

/// Add the preset's skills and subagents to what the config directory
/// gave: the built-in skills at least, and the roster after the
/// directory's subagents, which win by name.
pub(crate) fn add_roster(deep: Option<&DeepAgent>, project: &mut Loaded) {
    let Some(deep) = deep else {
        return;
    };
    project.skills.get_or_insert_with(Vec::new);
    let subagents = project.subagents.take().unwrap_or_default();
    project.subagents = Some(deep.subagents(subagents));
}
