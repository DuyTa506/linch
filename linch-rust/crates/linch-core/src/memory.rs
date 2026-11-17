//! Long-term memory: facts the model saves in one run and finds in a later
//! one, kept by a [`MemoryStore`] the host plugs in.
//!
//! A store is bound to one agent's namespace when it is built, so every call
//! is the namespace's own. Within it, a call names a *scope*: `None` is the
//! memory every session of the agent shares, `Some(key)` is one principal's
//! (see [`principal_scope`]). A scope never reads or writes another's rows.
//! The scope comes from the host, never from model input.
//!
//! Search is full text over content and tags, language neutral (no stemming),
//! and any matching word counts: a row that matches more of the query's
//! words ranks higher. A caller may ask for at least `min_match` of them.
//! [`search_terms`] is the one definition of those words, so every backend
//! reads a query the same way; [`recall_terms`] is the same without the
//! words too common to select anything, for the automatic recall. Text is
//! read in Unicode normal form C, so a decomposed query finds composed text.
//! Backends pass [`conformance::memory_store_suite`];
//! [`InMemoryMemoryStore`] is for tests and development, linch-postgres'
//! `PgMemoryStore` is the durable one.
//!
//! A store may cap its memories: one number for all its scopes, and one per
//! scope so a single principal (or a model that was told to save everything)
//! cannot fill the whole budget. [`default_scope_cap`] is the per-scope
//! number when none is named.

use async_trait::async_trait;
use sha2::{Digest, Sha256};
use thiserror::Error;
use unicode_normalization::UnicodeNormalization;

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

    /// A memory saved by one tool call, keyed by the call and by what it
    /// saves.
    ///
    /// The call's idempotency key alone is not enough: a provider that
    /// reuses a tool-use id for a later turn of the same run would have its
    /// second save silently answered with the first. With the text in the
    /// key, a replay of the same call (after a crash, or a kernel retry)
    /// still finds its row, and a different fact never does.
    ///
    /// Args:
    ///     call_key: The call's idempotency key (`run:tool_use_id`).
    ///     content: The text to remember.
    ///     tags: Labels, possibly empty.
    ///
    /// Raises:
    ///     InvalidMemory: As [`Self::new`].
    pub fn for_call(call_key: &str, content: &str, tags: &[String]) -> Result<Self, InvalidMemory> {
        let mut memory = Self::new(call_key, content, tags)?;
        let mut digest = Sha256::new();
        digest.update(memory.content.as_bytes());
        for tag in &memory.tags {
            digest.update([0]);
            digest.update(tag.as_bytes());
        }
        let digest = digest.finalize();
        let hex: String = digest.iter().take(12).map(|b| format!("{b:02x}")).collect();
        memory.key = format!("{call_key}:{hex}");
        Ok(memory)
    }
}

/// Why a store call failed.
#[derive(Debug, Error)]
pub enum MemoryError {
    /// The save would pass one of the store's caps.
    #[error("{}", limit_text(*.max, *.per_scope))]
    LimitReached {
        /// The cap that stopped the save.
        max: u64,
        /// Whether it is the cap of the one scope, not of the whole store.
        per_scope: bool,
    },
    /// The backend failed. The message may name the backend's address, so a
    /// host shows the model a fixed text instead.
    #[error("memory store failed: {0}")]
    Backend(#[source] Box<dyn std::error::Error + Send + Sync>),
}

fn limit_text(max: u64, per_scope: bool) -> String {
    match per_scope {
        true => format!("the memory limit of {max} for this scope is reached"),
        false => format!("the memory limit of {max} is reached"),
    }
}

/// The per-scope cap of a store capped at `max` that names none: a quarter
/// of it, and at least 1.
pub fn default_scope_cap(max: u64) -> u64 {
    (max / 4).max(1)
}

/// Where long-term memories are kept, for one agent's namespace.
///
/// Every call is bound to that namespace and to `scope`: `None` reads and
/// writes the memories every session shares, `Some(key)` those of one
/// principal, and neither sees the other's rows. `key` must not be empty.
#[async_trait]
pub trait MemoryStore: Send + Sync {
    /// Store `memory` in `scope`, or return the memory already stored under
    /// its key (which does not count against the caps).
    ///
    /// Raises:
    ///     MemoryError: `LimitReached` when the store's cap or the scope's
    ///         is full.
    async fn save(&self, scope: Option<&str>, memory: NewMemory) -> Result<Memory, MemoryError>;

    /// Up to `limit` memories of `scope` that match at least `min_match`
    /// words of `query` ([`search_terms`]; 0 and 1 both mean any word),
    /// best match first, newer first among equals. A query with no words
    /// matches nothing.
    async fn search(
        &self,
        scope: Option<&str>,
        query: &str,
        limit: usize,
        min_match: usize,
    ) -> Result<Vec<Memory>, MemoryError>;

    /// Delete memory `id` of `scope`. Returns whether it existed there; an
    /// id of another scope or namespace is not found.
    async fn delete(&self, scope: Option<&str>, id: &str) -> Result<bool, MemoryError>;

    /// Delete every memory of `scope`, for a host that forgets a principal.
    /// Returns how many there were. Other scopes are untouched.
    async fn purge(&self, scope: Option<&str>) -> Result<u64, MemoryError>;
}

/// The scope key of a principal's memories.
///
/// Length-prefixed, so no two principals share a key (`("a/b", "c")` and
/// `("a", "b/c")` differ).
pub fn principal_scope(principal: &Principal) -> String {
    let tenant = principal.tenant();
    format!("{}:{tenant}:{}", tenant.len(), principal.user())
}

/// The lower-cased runs of letters and digits of `text` in normal form C,
/// each cut at [`MAX_TERM_CHARS`] characters, repeats kept. Normalizing
/// first matters: a decomposed letter is a base and a combining mark, and
/// the mark is not alphanumeric, so the word would split in two.
fn words(text: &str) -> Vec<String> {
    let text: String = text.nfc().collect();
    (text.split(|c: char| !c.is_alphanumeric()))
        .filter(|word| !word.is_empty())
        .map(|word| word.to_lowercase().chars().take(MAX_TERM_CHARS).collect())
        .collect()
}

fn distinct(words: Vec<String>, keep: impl Fn(&str) -> bool, most: usize) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    for word in words {
        if keep(&word) && !terms.contains(&word) {
            terms.push(word);
        }
        if terms.len() == most {
            break;
        }
    }
    terms
}

/// The words a search reads in `query`: runs of letters and digits,
/// lower-cased, in normal form C, without repeats, at most
/// [`MAX_SEARCH_TERMS`] words of [`MAX_TERM_CHARS`] characters each, and
/// not `or`.
///
/// Nothing else of the query survives, so no backend ever sees an operator,
/// a quote or SQL from it.
pub fn search_terms(query: &str) -> Vec<String> {
    distinct(words(query), |word| word != "or", MAX_SEARCH_TERMS)
}

/// A few English function words, which match most memories and so select
/// none, and the Vietnamese particles that are not also content words.
const STOP_WORDS: &[&str] = &[
    "the", "and", "for", "are", "but", "not", "you", "was", "our", "has", "his", "her", "how",
    "its", "who", "did", "she", "can", "with", "that", "this", "have", "from", "they", "will",
    "would", "could", "should", "there", "their", "what", "about", "which", "when", "where",
    "your", "been", "were", "than", "then", "them", "these", "those", "some", "into", "does",
    "just", "also", "please", "và", "của", "là", "những", "các", "một", "này", "thì", "mà",
];

/// Whether `word` is too common to select a memory: a stop word, or an
/// ASCII word of letters under 3 characters. A short word with a diacritic
/// (`ăn`, `mẹ`) is a real Vietnamese word and stays.
fn is_noise(word: &str) -> bool {
    let short = word.chars().count() < 3 && word.chars().all(|c| c.is_ascii_alphabetic());
    short || STOP_WORDS.contains(&word)
}

/// The words the automatic recall searches with: [`search_terms`] without
/// the stop words and the short ASCII words, so a message like "what is the
/// plan for it" selects on `plan` and not on every memory that has an `is`.
/// The cap of [`MAX_SEARCH_TERMS`] counts the words that remain.
pub fn recall_terms(text: &str) -> Vec<String> {
    let keep = |word: &str| word != "or" && !is_noise(word);
    distinct(words(text), keep, MAX_SEARCH_TERMS)
}

/// Every word of `text` a search can find it by: the lower-cased runs of
/// letters and digits in normal form C, without repeats, each cut at
/// [`MAX_TERM_CHARS`] characters. A backend indexes these, so what
/// [`search_terms`] asks for is what was stored, whatever its own tokenizer
/// would have made of the text.
pub fn index_words(text: &str) -> Vec<String> {
    distinct(words(text), |_| true, usize::MAX)
}

#[cfg(test)]
mod tests;
