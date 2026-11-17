//! Long-term memory: facts the model saves in one run and finds in a later
//! one, kept by a [`MemoryStore`] the host plugs in.
//!
//! A store is bound to one agent's namespace when it is built, so every call
//! is the namespace's own. Within it, a call names a *scope*: `None` is the
//! memory every session of the agent shares, `Some(key)` is one principal's
//! (see [`principal_scope`]). A scope never reads or writes another's rows.
//! The scope comes from the host, never from model input.
//!
//! Search is full text over content and tags, language neutral (no stemming
//! or stop words), and any matching word counts: a row that matches more of
//! the query's words ranks higher. [`search_terms`] is the one definition of
//! those words, so every backend reads a query the same way. Backends pass
//! [`conformance::memory_store_suite`]; [`InMemoryMemoryStore`] is for tests
//! and development, linch-postgres' `PgMemoryStore` is the durable one.

use async_trait::async_trait;
use thiserror::Error;

use crate::policy::Principal;

pub mod conformance;
mod in_memory;

pub use in_memory::InMemoryMemoryStore;

/// Longest memory content, in bytes.
pub const MAX_CONTENT_BYTES: usize = 4096;
/// Most tags on one memory.
pub const MAX_TAGS: usize = 8;
/// Longest tag, in bytes.
pub const MAX_TAG_BYTES: usize = 64;
/// Longest search query, in bytes.
pub const MAX_QUERY_BYTES: usize = 1024;
/// Most results a search returns when the caller names no limit.
pub const DEFAULT_SEARCH_LIMIT: usize = 5;
/// Most results one search returns.
pub const MAX_SEARCH_LIMIT: usize = 20;
/// Most words of a query a search uses.
pub const MAX_SEARCH_TERMS: usize = 32;
/// Longest word of a query a search uses, in characters.
pub const MAX_TERM_CHARS: usize = 64;

/// A stored memory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Memory {
    /// Store-assigned id, which `delete` takes.
    pub id: String,
    /// The remembered text.
    pub content: String,
    /// Labels the search also reads.
    pub tags: Vec<String>,
    /// Creation time, Unix milliseconds.
    pub created_at_ms: u64,
}

/// A memory to store, checked by [`NewMemory::new`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewMemory {
    /// Names the write: saving the same key again returns the first row
    /// instead of storing a second (a replayed tool call).
    pub key: String,
    /// The text, trimmed, 1 to [`MAX_CONTENT_BYTES`] bytes.
    pub content: String,
    /// Trimmed, lower-cased, deduplicated, at most [`MAX_TAGS`].
    pub tags: Vec<String>,
}

/// Input a memory write refuses; the text is for the model.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct InvalidMemory(pub String);

impl NewMemory {
    /// Check and normalize a memory.
    ///
    /// Args:
    ///     key: Idempotency key of the write.
    ///     content: The text to remember.
    ///     tags: Labels, possibly empty.
    ///
    /// Raises:
    ///     InvalidMemory: Blank or too long content, a NUL byte anywhere, too
    ///         many tags, or a blank or too long tag.
    pub fn new(
        key: impl Into<String>,
        content: &str,
        tags: &[String],
    ) -> Result<Self, InvalidMemory> {
        let invalid = |message: String| Err(InvalidMemory(message));
        let content = content.trim();
        if content.is_empty() {
            return invalid("content must not be blank".into());
        }
        if content.len() > MAX_CONTENT_BYTES {
            return invalid(format!("content is over {MAX_CONTENT_BYTES} bytes"));
        }
        if content.contains('\0') {
            return invalid("content must not hold a NUL byte".into());
        }
        if tags.len() > MAX_TAGS {
            return invalid(format!("at most {MAX_TAGS} tags"));
        }
        let mut kept: Vec<String> = Vec::with_capacity(tags.len());
        for tag in tags {
            let tag = tag.trim().to_lowercase();
            if tag.is_empty() || tag.len() > MAX_TAG_BYTES {
                return invalid(format!("a tag must be 1 to {MAX_TAG_BYTES} bytes"));
            }
            if tag.contains('\0') {
                return invalid("a tag must not hold a NUL byte".into());
            }
            if !kept.contains(&tag) {
                kept.push(tag);
            }
        }
        Ok(Self {
            key: key.into(),
            content: content.to_owned(),
            tags: kept,
        })
    }
}

/// Why a store call failed.
#[derive(Debug, Error)]
pub enum MemoryError {
    /// The save would pass the store's cap on memories.
    #[error("the memory limit of {max} is reached")]
    LimitReached {
        /// The cap.
        max: u64,
    },
    /// The backend failed. The message may name the backend's address, so a
    /// host shows the model a fixed text instead.
    #[error("memory store failed: {0}")]
    Backend(#[source] Box<dyn std::error::Error + Send + Sync>),
}

/// Where long-term memories are kept, for one agent's namespace.
///
/// Every call is bound to that namespace and to `scope`: `None` reads and
/// writes the memories every session shares, `Some(key)` those of one
/// principal, and neither sees the other's rows. `key` must not be empty.
#[async_trait]
pub trait MemoryStore: Send + Sync {
    /// Store `memory` in `scope`, or return the memory already stored under
    /// its key (which does not count against the cap).
    ///
    /// Raises:
    ///     MemoryError: `LimitReached` when the store's cap is full.
    async fn save(&self, scope: Option<&str>, memory: NewMemory) -> Result<Memory, MemoryError>;

    /// Up to `limit` memories of `scope` that match any word of `query`
    /// ([`search_terms`]), best match first, newer first among equals. A
    /// query with no words matches nothing.
    async fn search(
        &self,
        scope: Option<&str>,
        query: &str,
        limit: usize,
    ) -> Result<Vec<Memory>, MemoryError>;

    /// Delete memory `id` of `scope`. Returns whether it existed there; an
    /// id of another scope or namespace is not found.
    async fn delete(&self, scope: Option<&str>, id: &str) -> Result<bool, MemoryError>;
}

/// The scope key of a principal's memories.
///
/// Length-prefixed, so no two principals share a key (`("a/b", "c")` and
/// `("a", "b/c")` differ).
pub fn principal_scope(principal: &Principal) -> String {
    let tenant = principal.tenant();
    format!("{}:{tenant}:{}", tenant.len(), principal.user())
}

/// The words a search reads in `query`: runs of letters and digits,
/// lower-cased, without repeats, at most [`MAX_SEARCH_TERMS`] words of
/// [`MAX_TERM_CHARS`] characters each, and not `or`.
///
/// Nothing else of the query survives, so no backend ever sees an operator,
/// a quote or SQL from it.
pub fn search_terms(query: &str) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    let words = query.split(|c: char| !c.is_alphanumeric());
    for word in words.filter(|word| !word.is_empty()) {
        let word: String = word.to_lowercase().chars().take(MAX_TERM_CHARS).collect();
        if word != "or" && !terms.contains(&word) {
            terms.push(word);
        }
        if terms.len() == MAX_SEARCH_TERMS {
            break;
        }
    }
    terms
}

/// Every word of `text` a search can find it by: the lower-cased runs of
/// letters and digits, without repeats, each cut at [`MAX_TERM_CHARS`]
/// characters. A backend indexes these, so what [`search_terms`] asks for is
/// what was stored, whatever its own tokenizer would have made of the text.
pub fn index_words(text: &str) -> Vec<String> {
    let mut words: Vec<String> = Vec::new();
    for word in text.split(|c: char| !c.is_alphanumeric()) {
        let word: String = word.to_lowercase().chars().take(MAX_TERM_CHARS).collect();
        if !word.is_empty() && !words.contains(&word) {
            words.push(word);
        }
    }
    words
}

#[cfg(test)]
mod tests;
