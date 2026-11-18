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
//!   `tool_execute` layers do **not** run on a hit: they wrap the tool's
//!   run, and no tool runs. A layer that must see every result (a
//!   fail-closed filter) belongs in `post_tool_use`.
//! - **What is stored** is the tool's own successful result, after the
//!   output schema check and the agent's tool-result redaction and before
//!   `post_tool_use`, for a read-scope tool named in [`ToolCache::tools`],
//!   never an error, never one larger than [`ToolCache::max_value_bytes`].
//!   When `tool_execute` layers are registered, a result is stored only if
//!   the layers returned it unchanged: a layer's veto or replacement is
//!   never cached, and a hit never serves output a layer had masked.
//!   **Without `spec.redaction` covering tool results, the result is stored
//!   as the tool returned it**, secrets included; a tenant server refuses
//!   a cache without that redaction.
//! - **The key** is the agent's namespace, the session
//!   ([`CacheScope::Session`], the default), the tool name and the
//!   canonical digest of the input. Read-scope tools may answer per session
//!   (a session's own files, its shell's home), so the default never serves
//!   one session another's result; [`CacheScope::Namespace`] shares entries
//!   across sessions, which is a promise that the tool answers the same
//!   for the same input whoever asks. The registry has no tool version or
//!   digest, so the key has none either: when a tool's behaviour changes
//!   (a new deploy), old entries live until their TTL; clear the namespace
//!   or change it. A tool that polls, asks the user, or says
//!   [`Tool::cache_safe`] is false (`Read`, which records what the session
//!   saw so `Edit` may follow) is never cached, and naming one in
//!   [`ToolCache::tools`] is refused at build.
//! - **A hit is visible**: the call's `tool_call_end` has `cache_hit`, and
//!   its start is written with that end, not before it, since nothing ran.
//! - **A write or exec call clears the namespace** (every session's
//!   entries), before it starts and after it ends, when
//!   [`ToolCache::invalidate_on_write`] is set (the default): a crash
//!   between the effect and a clear that only ran after it would leave
//!   stale rows in a durable store.
//! - **A read that began before a clear never stores its result.** Each
//!   namespace has an epoch that `clear` increments; a lookup returns the
//!   epoch it saw and the store accepts the later `put` only if it is
//!   still current, so a read that started before a write cannot store its
//!   pre-write answer after the write's clear.
//! - **A background launch ends caching for the agent.** A tool that
//!   starts work that outlives the call ([`Tool::launches_background`]:
//!   `Bash` with `run_in_background`, a background subagent) may change
//!   what a read tool answers at any later time, and nothing marks when.
//!   From the first such call the agent neither reads nor stores cache
//!   entries until it is dropped. Other servers sharing a Postgres cache
//!   do not know of the launch: leave `tool_cache` off an agent that runs
//!   background work in a workspace other servers cache.
//! - **A store failure or a timeout is a miss**, never model-visible text:
//!   every store call is bounded by [`STORE_TIMEOUT`]. A clear that fails
//!   also ends caching for the run's agent, as stale entries may remain.
//! - **Bytes are bounded** per namespace by [`ToolCache::max_entries`] and
//!   by [`ToolCache::max_value_bytes`] a result, and per group of
//!   namespaces by [`ToolCache::quota`] (the oldest entries go first); a
//!   tenant server sets the quota to the tenant's name, so a tenant's
//!   namespaces together stay within the server's cap.
//!
//! With Postgres stores the cache is a table shared by the namespace's
//! servers (`linch_postgres::PgToolCache`); otherwise it lives in the
//! agent ([`InMemoryToolCache`]).
//!
//! [`Tool::cache_safe`]: crate::Tool::cache_safe
//! [`Tool::launches_background`]: crate::Tool::launches_background

use std::collections::HashMap;
use std::fmt;
use std::sync::atomic::{AtomicBool, Ordering};
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

/// Longest one call of a [`ToolCacheStore`] may take before the scheduler
/// treats it as a miss.
pub const STORE_TIMEOUT: Duration = Duration::from_secs(2);

/// Whose results an entry may serve.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheScope {
    /// Only calls of the session that stored it. The default.
    #[default]
    Session,
    /// Calls of every session of the namespace.
    Namespace,
}

impl CacheScope {
    fn is_session(&self) -> bool {
        *self == Self::Session
    }
}

/// A cap on the bytes a group of namespaces may hold together.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ByteQuota {
    /// The group: every namespace equal to it, or under it as
    /// `group/...`. A tenant server writes the tenant's name here.
    pub group: String,
    /// Most bytes of result JSON the group's entries may total; the oldest
    /// entries go first.
    pub max_bytes: u64,
}

/// Which tool results an agent reuses. Set in `AgentSpec::tool_cache`;
/// absent, nothing is cached and the spec's wire form and contract are
/// those of a spec written before this existed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCache {
    /// Names of the cacheable tools. Required and never empty: the SDK
    /// cannot tell a deterministic tool from a volatile one. A tool in the
    /// list that is not read-scope is never cached; one that is not
    /// [`cache_safe`](crate::Tool::cache_safe) is refused at build.
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
    /// Whose results an entry serves. `session` when absent, and then off
    /// the wire.
    #[serde(default, skip_serializing_if = "CacheScope::is_session")]
    pub scope: CacheScope,
    /// A cap on the bytes a group of namespaces holds together; none when
    /// absent, and then off the wire.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quota: Option<ByteQuota>,
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
            scope: CacheScope::Session,
            quota: None,
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
        if let Some(quota) = &self.quota
            && (quota.group.trim().is_empty() || quota.max_bytes == 0)
        {
            return Some("tool_cache.quota needs a group and at least 1 byte");
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

/// What a lookup found: the live entry, if any, and the namespace's epoch
/// when the lookup ran, which a later [`ToolCacheStore::put`] must present.
#[derive(Debug, Clone, PartialEq)]
pub struct Lookup {
    /// The live entry under the key.
    pub hit: Option<ToolOutput>,
    /// The namespace's epoch: how many times it was cleared.
    pub epoch: u64,
}

/// What a [`ToolCacheStore::put`] must keep within.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PutLimits {
    /// How long the entry lives.
    pub ttl: Duration,
    /// Most entries of the namespace; the oldest go first.
    pub max_entries: u32,
    /// The group cap the store enforces after the put, if any.
    pub quota: Option<ByteQuota>,
}

/// Where cached results live, one key space per namespace.
///
/// An entry past its TTL is never returned. `put` also makes room: the
/// namespace holds at most `max_entries` and the quota's group at most
/// `max_bytes`, the oldest going first.
#[async_trait]
pub trait ToolCacheStore: Send + Sync {
    /// The live entry under `key`, if any, with the namespace's epoch.
    async fn get(&self, namespace: &str, key: &str) -> Result<Lookup, ToolCacheError>;

    /// Store `output` under `key`, replacing any entry there, only if the
    /// namespace is still at `epoch` (the one its lookup returned). A
    /// `clear` since then means the result may predate a write, so nothing
    /// is stored. Whether it was stored.
    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        epoch: u64,
        limits: &PutLimits,
    ) -> Result<bool, ToolCacheError>;

    /// Drop every entry of `namespace` and move it to the next epoch, so a
    /// `put` of a lookup made before it is refused.
    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError>;
}

/// The cache key of a call: the digest of the session (for
/// [`CacheScope::Session`]; `None` shares the key across sessions), the
/// tool name and the canonical input. `None` when the input is too deep to
/// digest, which makes the call uncacheable.
pub fn cache_key(session: Option<&str>, tool_name: &str, input: &JsonObject) -> Option<String> {
    let parts = match session {
        Some(session) => json!([session, tool_name, input]),
        None => json!([tool_name, input]),
    };
    let key = digest_with_domain(KEY_DOMAIN, &parts).ok()?;
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
    /// Set by a background launch or a failed clear: caching ends for the
    /// agent. Shared by every clone.
    stopped: Arc<AtomicBool>,
}

impl ToolCacheHandle {
    /// A handle on `store` for the agent of `namespace`.
    pub fn new(config: ToolCache, store: Arc<dyn ToolCacheStore>, namespace: String) -> Self {
        Self {
            config,
            store,
            namespace,
            stopped: Arc::new(AtomicBool::new(false)),
        }
    }

    /// End caching for the agent.
    pub(crate) fn stop(&self) {
        self.stopped.store(true, Ordering::Release);
    }

    /// Whether caching has ended for the agent.
    pub(crate) fn stopped(&self) -> bool {
        self.stopped.load(Ordering::Acquire)
    }
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
    /// Never removed: a clear that forgot its epoch would let a stale put
    /// through.
    epochs: HashMap<String, u64>,
    namespaces: HashMap<String, HashMap<String, Entry>>,
}

struct Entry {
    output: ToolOutput,
    expires: Instant,
    seq: u64,
    bytes: u64,
    group: Option<String>,
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

impl State {
    fn epoch(&self, namespace: &str) -> u64 {
        self.epochs.get(namespace).copied().unwrap_or(0)
    }

    /// Drop the newest-first entries of `group` past `max_bytes`.
    fn enforce(&mut self, quota: &ByteQuota, now: Instant) {
        let mut held = Vec::new();
        for (namespace, entries) in &self.namespaces {
            for (key, entry) in entries {
                if entry.group.as_deref() == Some(&quota.group) && entry.expires > now {
                    held.push((entry.seq, entry.bytes, namespace.clone(), key.clone()));
                }
            }
        }
        held.sort_unstable_by_key(|held| std::cmp::Reverse(held.0));
        let mut total = 0u64;
        for (_, bytes, namespace, key) in held {
            total = total.saturating_add(bytes);
            if total > quota.max_bytes
                && let Some(entries) = self.namespaces.get_mut(&namespace)
            {
                entries.remove(&key);
            }
        }
    }
}

#[async_trait]
impl ToolCacheStore for InMemoryToolCache {
    async fn get(&self, namespace: &str, key: &str) -> Result<Lookup, ToolCacheError> {
        let now = Instant::now();
        let mut state = self.lock();
        let epoch = state.epoch(namespace);
        let hit = match state.namespaces.get_mut(namespace) {
            None => None,
            Some(entries) => match entries.get(key) {
                Some(entry) if entry.expires > now => Some(entry.output.clone()),
                Some(_) => {
                    entries.remove(key);
                    None
                }
                None => None,
            },
        };
        Ok(Lookup { hit, epoch })
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        epoch: u64,
        limits: &PutLimits,
    ) -> Result<bool, ToolCacheError> {
        let bytes = serde_json::to_vec(output).map_or(u64::MAX, |bytes| bytes.len() as u64);
        let now = Instant::now();
        let mut state = self.lock();
        if state.epoch(namespace) != epoch {
            return Ok(false);
        }
        state.next += 1;
        let seq = state.next;
        let entries = state.namespaces.entry(namespace.to_owned()).or_default();
        entries.retain(|_, entry| entry.expires > now);
        let entry = Entry {
            output: output.clone(),
            expires: now + limits.ttl,
            seq,
            bytes,
            group: limits.quota.as_ref().map(|quota| quota.group.clone()),
        };
        entries.insert(key.to_owned(), entry);
        let room = usize::try_from(limits.max_entries).unwrap_or(usize::MAX);
        while entries.len() > room {
            let oldest = entries.iter().min_by_key(|(_, entry)| entry.seq);
            let Some(oldest) = oldest.map(|(key, _)| key.clone()) else {
                break;
            };
            entries.remove(&oldest);
        }
        if let Some(quota) = &limits.quota {
            state.enforce(quota, now);
        }
        Ok(true)
    }

    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError> {
        let mut state = self.lock();
        *state.epochs.entry(namespace.to_owned()).or_default() += 1;
        state.namespaces.remove(namespace);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn out(text: &str) -> ToolOutput {
        ToolOutput::new(text)
    }

    fn limits(ttl: Duration, max_entries: u32) -> PutLimits {
        PutLimits {
            ttl,
            max_entries,
            quota: None,
        }
    }

    async fn put(store: &InMemoryToolCache, ns: &str, key: &str, text: &str, room: u32) -> bool {
        let epoch = store.get(ns, key).await.unwrap().epoch;
        let limits = limits(Duration::from_secs(10), room);
        store
            .put(ns, key, &out(text), epoch, &limits)
            .await
            .unwrap()
    }

    async fn held(store: &InMemoryToolCache, ns: &str, key: &str) -> Option<ToolOutput> {
        store.get(ns, key).await.unwrap().hit
    }

    #[test]
    fn the_key_is_the_tool_and_the_canonical_input() {
        let a = json!({"x": 1, "y": [1, 2]});
        let b = json!({"y": [1, 2], "x": 1});
        let (a, b) = (a.as_object().unwrap(), b.as_object().unwrap());
        assert_eq!(cache_key(None, "Grep", a), cache_key(None, "Grep", b));
        assert_ne!(cache_key(None, "Grep", a), cache_key(None, "Glob", a));
        let other = json!({"x": 2, "y": [1, 2]});
        assert_ne!(
            cache_key(None, "Grep", a),
            cache_key(None, "Grep", other.as_object().unwrap())
        );
    }

    #[test]
    fn a_session_keys_its_entries_apart_and_a_shared_key_is_the_old_one() {
        let input = json!({"x": 1});
        let input = input.as_object().unwrap();
        assert_ne!(
            cache_key(Some("s1"), "Grep", input),
            cache_key(Some("s2"), "Grep", input)
        );
        assert_eq!(
            cache_key(Some("s1"), "Grep", input),
            cache_key(Some("s1"), "Grep", input)
        );
        assert_ne!(
            cache_key(Some("s1"), "Grep", input),
            cache_key(None, "Grep", input)
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
        let mut quota = ToolCache::new(["Grep"]);
        quota.quota = Some(ByteQuota {
            group: "acme".into(),
            max_bytes: 0,
        });
        assert!(quota.problem().is_some());
        quota.quota = Some(ByteQuota {
            group: " ".into(),
            max_bytes: 9,
        });
        assert!(quota.problem().is_some());
        assert!(
            serde_json::from_str::<ToolCache>("{}").is_err(),
            "tools is required"
        );
        assert!(serde_json::from_str::<ToolCache>(r#"{"tools":["a"],"x":1}"#).is_err());
    }

    #[test]
    fn the_default_scope_and_no_quota_stay_off_the_wire() {
        let cache = ToolCache::new(["Grep"]);
        let wire = serde_json::to_value(&cache).unwrap();
        // Exactly the shape (and so the contract digest) a cache had before
        // `scope` and `quota` existed.
        assert_eq!(
            wire,
            json!({"tools": ["Grep"], "ttl_ms": 300_000, "max_entries": 256,
                   "max_value_bytes": 100_000, "invalidate_on_write": true})
        );
        let shared: ToolCache =
            serde_json::from_str(r#"{"tools":["a"],"scope":"namespace"}"#).unwrap();
        assert_eq!(shared.scope, CacheScope::Namespace);
        assert_eq!(serde_json::to_value(&shared).unwrap()["scope"], "namespace");
        assert!(serde_json::from_str::<ToolCache>(r#"{"tools":["a"],"scope":"x"}"#).is_err());
    }

    #[tokio::test(start_paused = true)]
    async fn entries_expire_and_the_oldest_go_first() {
        let store = InMemoryToolCache::new();
        for (key, text) in [("a", "A"), ("b", "B"), ("c", "C")] {
            assert!(put(&store, "ns", key, text, 2).await);
        }
        assert_eq!(held(&store, "ns", "a").await, None, "the oldest went");
        assert_eq!(held(&store, "ns", "c").await, Some(out("C")));
        tokio::time::advance(Duration::from_secs(11)).await;
        assert_eq!(held(&store, "ns", "c").await, None, "past its ttl");
    }

    #[tokio::test]
    async fn namespaces_never_see_each_other_and_clear_is_per_namespace() {
        let store = InMemoryToolCache::new();
        assert!(put(&store, "one", "k", "1", 8).await);
        assert!(put(&store, "two", "k", "2", 8).await);
        assert_eq!(held(&store, "one", "k").await, Some(out("1")));
        assert_eq!(held(&store, "two", "k").await, Some(out("2")));
        store.clear("one").await.unwrap();
        assert_eq!(held(&store, "one", "k").await, None);
        assert_eq!(held(&store, "two", "k").await, Some(out("2")));
    }

    /// A read that began before a write must not store its pre-write answer
    /// after the write's clear.
    #[tokio::test]
    async fn a_put_from_before_a_clear_is_refused() {
        let store = InMemoryToolCache::new();
        let before = store.get("ns", "k").await.unwrap();
        assert_eq!(before.epoch, 0);
        store.clear("ns").await.unwrap();
        let (old, room) = (out("old"), limits(LONG, 8));
        let stale = store.put("ns", "k", &old, before.epoch, &room);
        assert!(!stale.await.unwrap(), "the clear moved the epoch on");
        assert_eq!(held(&store, "ns", "k").await, None);
        // A lookup made after the clear stores, and a second clear moves on
        // again: the epoch is never forgotten.
        let after = store.get("ns", "k").await.unwrap();
        assert_eq!(after.epoch, 1);
        let new = out("new");
        assert!(
            store
                .put("ns", "k", &new, after.epoch, &room)
                .await
                .unwrap()
        );
        store.clear("ns").await.unwrap();
        assert_eq!(store.get("ns", "k").await.unwrap().epoch, 2);
    }

    const LONG: Duration = Duration::from_secs(60);

    /// A group's namespaces together stay within the cap, the oldest entry
    /// going first, and another group is left alone.
    #[tokio::test]
    async fn a_quota_bounds_a_group_of_namespaces_together() {
        let store = InMemoryToolCache::new();
        let size = serde_json::to_vec(&out("x")).unwrap().len() as u64;
        let quota = |group: &str| PutLimits {
            ttl: LONG,
            max_entries: 8,
            quota: Some(ByteQuota {
                group: group.into(),
                max_bytes: size * 2,
            }),
        };
        for (ns, key, group) in [
            ("acme/1", "a", "acme"),
            ("acme/2", "b", "acme"),
            ("other", "c", "other"),
            ("acme/1", "d", "acme"),
        ] {
            let epoch = store.get(ns, key).await.unwrap().epoch;
            let (value, limits) = (out("x"), quota(group));
            assert!(store.put(ns, key, &value, epoch, &limits).await.unwrap());
        }
        assert_eq!(held(&store, "acme/1", "a").await, None, "the oldest went");
        assert!(held(&store, "acme/2", "b").await.is_some());
        assert!(held(&store, "acme/1", "d").await.is_some());
        assert!(held(&store, "other", "c").await.is_some());
    }
}
