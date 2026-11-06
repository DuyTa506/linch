//! The workers of each parent session, in process memory: their display
//! names, and the children a closed session leaves behind.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};

use super::SubagentDefinition;
use crate::ids::{RunId, SessionId};

/// One worker a parent session started: a subagent's child run, whose id
/// is also its session's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Worker {
    pub(crate) id: RunId,
    /// The definition's name, or `Agent #N` for the N-th general-purpose
    /// worker of the session.
    pub(crate) display_name: String,
    /// The definition the worker runs.
    pub(crate) definition: SubagentDefinition,
}

/// The workers of every parent session of one agent.
#[derive(Debug, Default)]
pub(crate) struct Registry(Mutex<HashMap<SessionId, Known>>);

#[derive(Debug, Default)]
struct Known {
    /// General-purpose workers named so far.
    generals: u32,
    workers: Vec<Worker>,
}

impl Registry {
    /// The worker `id` of `session`, registered now with its display name
    /// if it is new. `general`: the definition is the general-purpose one.
    pub(crate) fn enlist(
        &self,
        session: &SessionId,
        id: &RunId,
        definition: &SubagentDefinition,
        general: bool,
    ) -> Worker {
        let mut sessions = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let known = sessions.entry(session.clone()).or_default();
        if let Some(worker) = known.workers.iter().find(|worker| worker.id == *id) {
            return worker.clone();
        }
        let display_name = match general {
            true => {
                known.generals += 1;
                format!("Agent #{}", known.generals)
            }
            false => definition.name.clone(),
        };
        let worker = Worker {
            id: id.clone(),
            display_name,
            definition: definition.clone(),
        };
        known.workers.push(worker.clone());
        worker
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn general_workers_are_numbered_per_session_and_named_once() {
        let registry = Registry::default();
        let general = SubagentDefinition::new("_default", "", "");
        let scout = SubagentDefinition::new("scout", "", "");
        let (s1, s2) = (SessionId::new("s1"), SessionId::new("s2"));
        let name = |session, id: &str, definition, general| {
            (registry.enlist(session, &RunId::new(id), definition, general)).display_name
        };
        assert_eq!(name(&s1, "a", &general, true), "Agent #1");
        assert_eq!(name(&s1, "b", &scout, false), "scout");
        assert_eq!(name(&s1, "c", &general, true), "Agent #2");
        assert_eq!(name(&s1, "a", &general, true), "Agent #1", "again");
        assert_eq!(name(&s2, "d", &general, true), "Agent #1");
    }
}
