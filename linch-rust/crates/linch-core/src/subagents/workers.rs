//! The workers of each parent session, in process memory: their display
//! names, which ones the model may address, and the child sessions a
//! closed session leaves behind.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

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
    /// Whether `SubagentContinue` and `TaskStop` may name it: a background
    /// worker, or a foreground one the agent keeps.
    pub(crate) addressable: bool,
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
    fn lock(&self) -> MutexGuard<'_, HashMap<SessionId, Known>> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The worker `id` of `session`, registered now with its display name
    /// if it is new. `general`: the definition is the general-purpose one.
    pub(crate) fn enlist(
        &self,
        session: &SessionId,
        id: &RunId,
        definition: &SubagentDefinition,
        general: bool,
        addressable: bool,
    ) -> Worker {
        let mut sessions = self.lock();
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
            addressable,
        };
        known.workers.push(worker.clone());
        worker
    }

    /// The addressable worker of `session` whose id is `target`, else the
    /// only one whose display name is.
    ///
    /// Raises:
    ///     String: The model-facing error when several workers have that
    ///         display name.
    pub(crate) fn resolve(
        &self,
        session: &SessionId,
        target: &str,
    ) -> Result<Option<Worker>, String> {
        let sessions = self.lock();
        let Some(known) = sessions.get(session) else {
            return Ok(None);
        };
        let workers = known.workers.iter().filter(|worker| worker.addressable);
        if let Some(worker) = workers.clone().find(|worker| worker.id.as_str() == target) {
            return Ok(Some(worker.clone()));
        }
        let named: Vec<&Worker> = workers.filter(|w| w.display_name == target).collect();
        match named.as_slice() {
            [] => Ok(None),
            [worker] => Ok(Some((*worker).clone())),
            several => {
                let ids: Vec<&str> = several.iter().map(|w| w.id.as_str()).collect();
                Err(format!(
                    "Worker display name '{target}' is ambiguous; use a worker id: {}.",
                    ids.join(", ")
                ))
            }
        }
    }

    /// The ids of the addressable workers of `session`, in start order.
    pub(crate) fn ids(&self, session: &SessionId) -> Vec<String> {
        let sessions = self.lock();
        let workers = sessions.get(session).map(|known| known.workers.iter());
        let addressable = workers.into_iter().flatten().filter(|w| w.addressable);
        addressable.map(|worker| worker.id.to_string()).collect()
    }

    /// Forget the worker `id` of `session`, whose session was closed; its
    /// display name stays taken.
    pub(crate) fn forget(&self, session: &SessionId, id: &RunId) {
        if let Some(known) = self.lock().get_mut(session) {
            known.workers.retain(|worker| worker.id != *id);
        }
    }

    /// Forget `session`; returns its workers' session ids.
    pub(crate) fn close(&self, session: &SessionId) -> Vec<SessionId> {
        let known = self.lock().remove(session);
        let workers = known.map(|known| known.workers).unwrap_or_default();
        let ids = workers.into_iter().map(|w| SessionId::new(w.id.as_str()));
        ids.collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workers_are_named_per_session_and_found_by_id_or_unique_name() {
        let registry = Registry::default();
        let general = SubagentDefinition::new("_default", "", "");
        let scout = SubagentDefinition::new("scout", "", "");
        let (s1, s2) = (SessionId::new("s1"), SessionId::new("s2"));
        let enlist = |session, id: &str, definition, general| {
            let worker = registry.enlist(session, &RunId::new(id), definition, general, true);
            worker.display_name
        };
        assert_eq!(enlist(&s1, "a", &general, true), "Agent #1");
        assert_eq!(enlist(&s1, "b", &scout, false), "scout");
        assert_eq!(enlist(&s1, "c", &general, true), "Agent #2");
        assert_eq!(enlist(&s1, "a", &general, true), "Agent #1", "again");
        assert_eq!(enlist(&s2, "d", &general, true), "Agent #1");
        let hidden = registry.enlist(&s1, &RunId::new("e"), &scout, false, false);
        assert!(!hidden.addressable);

        let found = |target| {
            registry
                .resolve(&s1, target)
                .map(|w| w.map(|w| w.id.to_string()))
        };
        assert_eq!(found("c"), Ok(Some("c".into())));
        assert_eq!(found("Agent #2"), Ok(Some("c".into())));
        assert_eq!(found("scout"), Ok(Some("b".into())), "e is not addressable");
        assert_eq!(found("e"), Ok(None));
        let _ = registry.enlist(&s1, &RunId::new("f"), &scout, false, true);
        assert_eq!(
            found("scout"),
            Err("Worker display name 'scout' is ambiguous; use a worker id: b, f.".into())
        );
        assert_eq!(registry.ids(&s1), ["a", "b", "c", "f"]);
        let closed: Vec<String> = registry.close(&s1).iter().map(|s| s.to_string()).collect();
        assert_eq!(closed, ["a", "b", "c", "e", "f"]);
        assert!(registry.ids(&s1).is_empty());
    }
}
