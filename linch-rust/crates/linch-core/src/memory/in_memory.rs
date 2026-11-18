//! [`InMemoryMemoryStore`]: memories in process memory, for tests and
//! development.

use std::collections::HashSet;
use std::sync::{Mutex, PoisonError};
use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;

use super::{
    Memory, MemoryError, MemoryStore, NewMemory, default_scope_cap, index_words, search_terms,
};

struct Row {
    scope: Option<String>,
    key: String,
    memory: Memory,
    /// Lower-cased words of the content and tags.
    words: HashSet<String>,
}

/// A [`MemoryStore`] in process memory, for one agent. Everything is lost
/// on exit; a durable agent uses linch-postgres' `PgMemoryStore`.
#[derive(Default)]
pub struct InMemoryMemoryStore {
    rows: Mutex<Vec<Row>>,
    cap: Option<u64>,
    scope_cap: Option<u64>,
}

impl InMemoryMemoryStore {
    /// An empty store without a cap.
    pub fn new() -> Self {
        Self::default()
    }

    /// This store refusing a new memory once it holds `max`, in every
    /// scope. Each scope is held to [`default_scope_cap`] of it, unless
    /// [`Self::with_scope_cap`] names another.
    pub fn with_cap(mut self, max: u64) -> Self {
        self.cap = Some(max);
        self
    }

    /// This store refusing a new memory once its scope holds `max` of its
    /// own. It counts only with [`Self::with_cap`].
    pub fn with_scope_cap(mut self, max: u64) -> Self {
        self.scope_cap = Some(max);
        self
    }
}

fn now_ms() -> u64 {
    let since = SystemTime::now().duration_since(UNIX_EPOCH);
    since.map_or(0, |elapsed| {
        u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
    })
}

#[async_trait]
impl MemoryStore for InMemoryMemoryStore {
    async fn save(&self, scope: Option<&str>, memory: NewMemory) -> Result<Memory, MemoryError> {
        let mut rows = self.rows.lock().unwrap_or_else(PoisonError::into_inner);
        let held = |row: &&Row| row.scope.as_deref() == scope && row.key == memory.key;
        if let Some(row) = rows.iter().find(held) {
            return Ok(row.memory.clone());
        }
        if let Some(max) = self.cap {
            let own = rows.iter().filter(|row| row.scope.as_deref() == scope);
            let each = self.scope_cap.unwrap_or_else(|| default_scope_cap(max));
            if own.count() as u64 >= each {
                return Err(MemoryError::LimitReached {
                    max: each,
                    per_scope: true,
                });
            }
            if rows.len() as u64 >= max {
                return Err(MemoryError::LimitReached {
                    max,
                    per_scope: false,
                });
            }
        }
        let mut words = index_words(&memory.content)
            .into_iter()
            .collect::<HashSet<_>>();
        words.extend(memory.tags.iter().flat_map(|tag| index_words(tag)));
        let stored = Memory {
            id: uuid::Uuid::new_v4().simple().to_string(),
            content: memory.content,
            tags: memory.tags,
            created_at_ms: now_ms().max(1),
        };
        rows.push(Row {
            scope: scope.map(str::to_owned),
            key: memory.key,
            memory: stored.clone(),
            words,
        });
        Ok(stored)
    }

    async fn search(
        &self,
        scope: Option<&str>,
        query: &str,
        limit: usize,
        min_match: usize,
    ) -> Result<Vec<Memory>, MemoryError> {
        let terms = search_terms(query);
        let rows = self.rows.lock().unwrap_or_else(PoisonError::into_inner);
        let needed = min_match.max(1);
        // Row order is save order, so a later row is newer among equals.
        let mut scored: Vec<(usize, usize, &Memory)> = (rows.iter().enumerate())
            .filter(|(_, row)| row.scope.as_deref() == scope)
            .filter_map(|(at, row)| {
                let score = terms.iter().filter(|t| row.words.contains(*t)).count();
                (score >= needed).then_some((score, at, &row.memory))
            })
            .collect();
        scored.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.cmp(&a.1)));
        Ok(scored
            .into_iter()
            .take(limit)
            .map(|(_, _, memory)| memory.clone())
            .collect())
    }

    async fn delete(&self, scope: Option<&str>, id: &str) -> Result<bool, MemoryError> {
        let mut rows = self.rows.lock().unwrap_or_else(PoisonError::into_inner);
        let before = rows.len();
        rows.retain(|row| !(row.scope.as_deref() == scope && row.memory.id == id));
        Ok(rows.len() < before)
    }

    async fn purge(&self, scope: Option<&str>) -> Result<u64, MemoryError> {
        let mut rows = self.rows.lock().unwrap_or_else(PoisonError::into_inner);
        let before = rows.len();
        rows.retain(|row| row.scope.as_deref() != scope);
        Ok((before - rows.len()) as u64)
    }
}
