//! [`MemoryBackend`] and [`MemorySessions`]: files in memory, one backend
//! per session.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;
use linch_core::SessionId;

use super::{FileBackend, SessionFiles, VfsError, apply_edit, file_path, is_under, normalize_path};

/// Files in a map in memory. Nothing outlives the value.
///
/// Cheap to create; [`MemorySessions`] keeps one per session.
#[derive(Debug, Default)]
pub struct MemoryBackend {
    files: Mutex<BTreeMap<String, String>>,
}

impl MemoryBackend {
    /// An empty backend.
    pub fn new() -> Self {
        Self::default()
    }

    fn files(&self) -> MutexGuard<'_, BTreeMap<String, String>> {
        // No code panics while holding the lock, so a poisoned map is
        // still consistent.
        self.files.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[async_trait]
impl FileBackend for MemoryBackend {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        let path = file_path(path)?;
        self.files()
            .get(&path)
            .cloned()
            .ok_or(VfsError::NotFound { path })
    }

    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        let path = file_path(path)?;
        self.files().insert(path, content.to_owned());
        Ok(())
    }

    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError> {
        let path = file_path(path)?;
        let mut files = self.files();
        let Some(text) = files.get_mut(&path) else {
            return Err(VfsError::NotFound { path });
        };
        let (updated, count) = apply_edit(&path, text, old, new, replace_all)?;
        *text = updated;
        Ok(count)
    }

    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        let prefix = normalize_path(prefix)?;
        let files = self.files();
        Ok(files
            .keys()
            .filter(|path| is_under(path, &prefix))
            .cloned()
            .collect())
    }

    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        let path = file_path(path)?;
        Ok(self.files().contains_key(&path))
    }

    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        let path = file_path(path)?;
        self.files().remove(&path);
        Ok(())
    }
}

/// A fresh [`MemoryBackend`] per session, made on first use (Python's
/// default session filesystem).
///
/// The host calls [`Self::forget`] when a session ends, for example from
/// `AgentBuilder::on_session_close`; until then a session's files stay in
/// memory.
#[derive(Debug, Default)]
pub struct MemorySessions {
    sessions: Mutex<HashMap<SessionId, Arc<MemoryBackend>>>,
}

impl MemorySessions {
    /// No sessions yet.
    pub fn new() -> Self {
        Self::default()
    }

    /// Drop the files of `session_id`. Its next use starts empty.
    pub fn forget(&self, session_id: &SessionId) {
        self.sessions().remove(session_id);
    }

    fn sessions(&self) -> MutexGuard<'_, HashMap<SessionId, Arc<MemoryBackend>>> {
        self.sessions.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl SessionFiles for MemorySessions {
    fn backend(&self, session_id: &SessionId) -> Arc<dyn FileBackend> {
        let mut sessions = self.sessions();
        Arc::clone(sessions.entry(session_id.clone()).or_default()) as Arc<dyn FileBackend>
    }
}
