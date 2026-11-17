//! `tool_cache`: reuse the result of a read tool for the same input.
//!
//! Python's `ToolCacheHook` keeps a per-run, in-memory cache of read-scope
//! tool results, served through a `PreToolUse` resolve. Here the cache is
//! per agent namespace and may be durable, and it sits in the scheduler
//! after the call is planned, so it cannot skip what planning decides:
//!
//! - **Permission and `pre_tool_use` run first.** A call permission denied
//!   or a hook blocked never reaches the cache. A hit then replaces only
//!   the tool's own run.
//! - **`post_tool_use`, redaction and the output schema run on a hit** as
//!   on a fresh result, so a hook sees every result the model sees.
//! - **What is stored** is a successful result of a read-scope tool named in
//!   [`ToolCache::tools`], after the schema check and redaction and before
//!   `post_tool_use`, never an error, and never one larger than
//!   [`ToolCache::max_value_bytes`]. A durable table therefore never holds
//!   unredacted output, and hooks run again on every hit.
//! - **The key** is the agent's namespace, the tool name and the canonical
//!   digest of the input. Entries are shared by every session and run of
//!   the namespace: naming a tool is a promise that it answers the same for
//!   the same input whoever asks. A tool that polls or asks the user is
//!   never cached.
//! - **A hit is visible**: the call's `tool_call_end` has `cache_hit`, and
//!   its start is written with that end, not before it, since nothing ran.
//! - **A write or exec call clears the namespace**, before it starts and
//!   after it ends, when [`ToolCache::invalidate_on_write`] is set (the
//!   default): a crash between the effect and a clear that only ran after
//!   it would leave stale rows in a durable store.
//! - **A store failure is a miss.** It is never model-visible text.
//!
//! With Postgres stores the cache is a table shared by the namespace's
//! servers (`linch_postgres::PgToolCache`); otherwise it lives in the
//! agent ([`InMemoryToolCache`]).

use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::json;
use thiserror::Error;
use tokio::time::Instant;

use crate::canonical::digest_with_domain;
use crate::json::JsonObject;
use crate::tool::ToolOutput;

/// Domain separator of a cache key's digest.
const KEY_DOMAIN: &str = "linch.tool_cache.key";

/// Which tool results an agent reuses. Set in `AgentSpec::tool_cache`;
/// absent, nothing is cached and the spec's wire form and contract are
/// those of a spec written before this existed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCache {
    /// Names of the cacheable tools. Required and never empty: the SDK
    /// cannot tell a deterministic tool from a volatile one. A tool in the
    /// list that is not read-scope is never cached.
    pub tools: Vec<String>,
    /// How long an entry lives, in milliseconds.
    #[serde(default = "default_ttl_ms")]
    pub ttl_ms: u64,
    /// Most entries of the namespace; the oldest go first.
    #[serde(default = "default_max_entries")]
    pub max_entries: u32,
    /// Results whose JSON is larger are not stored.
    #[serde(default = "default_max_value_bytes")]
    pub max_value_bytes: u64,
    /// Whether a write or exec tool's call clears the namespace's entries,
    /// before it starts and after it ends.
    #[serde(default = "yes")]
    pub invalidate_on_write: bool,
}

fn default_ttl_ms() -> u64 {
    300_000
}

fn default_max_entries() -> u32 {
    256
}

fn default_max_value_bytes() -> u64 {
    100_000
}

fn yes() -> bool {
    true
}

impl ToolCache {
    /// A cache of `tools` with every other field at its default.
    pub fn new(tools: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            tools: tools.into_iter().map(Into::into).collect(),
            ttl_ms: default_ttl_ms(),
            max_entries: default_max_entries(),
            max_value_bytes: default_max_value_bytes(),
            invalidate_on_write: true,
        }
    }

    /// Why this cache cannot run, if it cannot.
    pub fn problem(&self) -> Option<&'static str> {
        if self.tools.is_empty() || self.tools.iter().any(|name| name.trim().is_empty()) {
            return Some("tool_cache.tools needs at least one name and no blank one");
        }
        if self.ttl_ms == 0 || self.max_entries == 0 || self.max_value_bytes == 0 {
            return Some("tool_cache.ttl_ms, max_entries and max_value_bytes must be at least 1");
        }
        None
    }

    /// How long an entry lives.
    pub fn ttl(&self) -> Duration {
        Duration::from_millis(self.ttl_ms)
    }

    /// Whether `name` is one of the cacheable tools.
    pub(crate) fn names(&self, name: &str) -> bool {
        self.tools.iter().any(|listed| listed == name)
    }
}

/// The store failed. Opaque on purpose: what it says is for the host's
/// log, never for the model.
#[derive(Debug, Error)]
#[error("the tool cache store failed")]
pub struct ToolCacheError(#[source] pub Box<dyn std::error::Error + Send + Sync>);

/// Where cached results live, one key space per namespace.
///
/// An entry past its TTL is never returned. `put` also makes room: the
/// namespace holds at most `max_entries`, the oldest going first.
#[async_trait]
pub trait ToolCacheStore: Send + Sync {
    /// The live entry under `key`, if any.
    async fn get(&self, namespace: &str, key: &str) -> Result<Option<ToolOutput>, ToolCacheError>;

    /// Store `output` under `key` for `ttl`, replacing any entry there.
    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        ttl: Duration,
        max_entries: u32,
    ) -> Result<(), ToolCacheError>;

    /// Drop every entry of `namespace`.
    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError>;
}

/// The cache key of a call: the digest of the tool name and the canonical
/// input. `None` when the input is too deep to digest, which makes the
/// call uncacheable.
pub fn cache_key(tool_name: &str, input: &JsonObject) -> Option<String> {
    let key = digest_with_domain(KEY_DOMAIN, &json!([tool_name, input])).ok()?;
    Some(key.to_string())
}

/// What a run needs to use the cache: the policy, the store and the
/// agent's namespace.
#[derive(Clone)]
pub struct ToolCacheHandle {
    /// The agent's policy.
    pub config: ToolCache,
    /// Where entries live.
    pub store: Arc<dyn ToolCacheStore>,
    /// The agent's namespace, the key space of its entries.
    pub namespace: String,
}

impl fmt::Debug for ToolCacheHandle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ToolCacheHandle")
            .field("config", &self.config)
            .field("namespace", &self.namespace)
            .finish_non_exhaustive()
    }
}

/// A [`ToolCacheStore`] in the process's memory, for memory stores and
/// tests. Time is tokio's, so a test may pause and advance it.
#[derive(Default)]
pub struct InMemoryToolCache {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    next: u64,
    namespaces: HashMap<String, HashMap<String, Entry>>,
}

struct Entry {
    output: ToolOutput,
    expires: Instant,
    seq: u64,
}

impl InMemoryToolCache {
    /// An empty cache.
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[async_trait]
impl ToolCacheStore for InMemoryToolCache {
    async fn get(&self, namespace: &str, key: &str) -> Result<Option<ToolOutput>, ToolCacheError> {
        let now = Instant::now();
        let mut state = self.lock();
        let Some(entries) = state.namespaces.get_mut(namespace) else {
            return Ok(None);
        };
        match entries.get(key) {
            Some(entry) if entry.expires > now => Ok(Some(entry.output.clone())),
            Some(_) => {
                entries.remove(key);
                Ok(None)
            }
            None => Ok(None),
        }
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        ttl: Duration,
        max_entries: u32,
    ) -> Result<(), ToolCacheError> {
        let now = Instant::now();
        let mut state = self.lock();
        state.next += 1;
        let seq = state.next;
        let entries = state.namespaces.entry(namespace.to_owned()).or_default();
        entries.retain(|_, entry| entry.expires > now);
        let entry = Entry {
            output: output.clone(),
            expires: now + ttl,
            seq,
        };
        entries.insert(key.to_owned(), entry);
        let room = usize::try_from(max_entries).unwrap_or(usize::MAX);
        while entries.len() > room {
            let oldest = entries.iter().min_by_key(|(_, entry)| entry.seq);
            let Some(oldest) = oldest.map(|(key, _)| key.clone()) else {
                break;
            };
            entries.remove(&oldest);
        }
        Ok(())
    }

    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError> {
        self.lock().namespaces.remove(namespace);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn out(text: &str) -> ToolOutput {
        ToolOutput::new(text)
    }

    #[test]
    fn the_key_is_the_tool_and_the_canonical_input() {
        let a = json!({"x": 1, "y": [1, 2]});
        let b = json!({"y": [1, 2], "x": 1});
        let (a, b) = (a.as_object().unwrap(), b.as_object().unwrap());
        assert_eq!(cache_key("Grep", a), cache_key("Grep", b));
        assert_ne!(cache_key("Grep", a), cache_key("Glob", a));
        let other = json!({"x": 2, "y": [1, 2]});
        assert_ne!(
            cache_key("Grep", a),
            cache_key("Grep", other.as_object().unwrap())
        );
    }

    #[test]
    fn a_cache_must_name_a_tool_and_have_room() {
        assert_eq!(ToolCache::new(["Grep"]).problem(), None);
        assert!(ToolCache::new(Vec::<String>::new()).problem().is_some());
        assert!(ToolCache::new([" "]).problem().is_some());
        let mut cache = ToolCache::new(["Grep"]);
        cache.max_entries = 0;
        assert!(cache.problem().is_some());
        assert!(
            serde_json::from_str::<ToolCache>("{}").is_err(),
            "tools is required"
        );
        assert!(serde_json::from_str::<ToolCache>(r#"{"tools":["a"],"x":1}"#).is_err());
    }

    #[tokio::test(start_paused = true)]
    async fn entries_expire_and_the_oldest_go_first() {
        let store = InMemoryToolCache::new();
        let ttl = Duration::from_secs(10);
        store.put("ns", "a", &out("A"), ttl, 2).await.unwrap();
        store.put("ns", "b", &out("B"), ttl, 2).await.unwrap();
        store.put("ns", "c", &out("C"), ttl, 2).await.unwrap();
        assert_eq!(
            store.get("ns", "a").await.unwrap(),
            None,
            "the oldest was evicted"
        );
        assert_eq!(store.get("ns", "c").await.unwrap(), Some(out("C")));
        tokio::time::advance(Duration::from_secs(11)).await;
        assert_eq!(store.get("ns", "c").await.unwrap(), None, "past its ttl");
    }

    #[tokio::test]
    async fn namespaces_never_see_each_other_and_clear_is_per_namespace() {
        let store = InMemoryToolCache::new();
        let ttl = Duration::from_secs(60);
        store.put("one", "k", &out("1"), ttl, 8).await.unwrap();
        store.put("two", "k", &out("2"), ttl, 8).await.unwrap();
        assert_eq!(store.get("one", "k").await.unwrap(), Some(out("1")));
        assert_eq!(store.get("two", "k").await.unwrap(), Some(out("2")));
        store.clear("one").await.unwrap();
        assert_eq!(store.get("one", "k").await.unwrap(), None);
        assert_eq!(store.get("two", "k").await.unwrap(), Some(out("2")));
    }
}
