# Scoped, bounded SWR memory prefetch

Status: implementation specification. This is an opt-in, Python-native memory
adapter slice. It keeps the existing `MemoryStore` protocol and ordinary
`MemoryContextBuilder` behavior unchanged.

## 1. Goal and boundary

`MemoryContextBuilder` currently awaits `store.search()` for every provider
call. This feature lets an authenticated host opt into a bounded,
stale-while-revalidate (SWR) cache for automatic memory context:

- a fresh cached result is injected immediately;
- a stale result is injected immediately while one bounded refresh runs;
- a cold, expired, saturated, cancelled, or failed lookup injects no memory
  rather than delaying the model call; and
- all cache state is in-memory only, so process resume is a safe cache miss.

This feature does not change prompts, `MemoryStore`, retrieval ranking,
provider calls, persisted session history, compaction, or manual memory-tool
semantics. A host that requires same-turn, authoritative recall keeps using
the existing direct `MemoryContextBuilder` path (`prefetch=False`).

There is no process-global cache, background service, scheduler, database
schema, worker process, or OpenHuman/Tiny* code. A host explicitly owns the
cache instance and its trusted scope binding.

## 2. Public API

Add these public types from `linch.memory` and the root `linch` surface.

```python
@dataclass(frozen=True, slots=True)
class MemoryScope:
    tenant_id: str
    namespace: str
    policy_id: str | None = None
    policy_revision: str | int | None = None


@dataclass(frozen=True, slots=True)
class MemoryPrefetchConfig:
    fresh_ttl_s: float = 5.0
    stale_grace_s: float = 30.0
    max_entries: int = 128
    max_bytes: int = 1_048_576
    max_inflight: int = 8


class MemoryPrefetchCache:
    def __init__(self, config: MemoryPrefetchConfig | None = None) -> None: ...
    def invalidate(self, scope: MemoryScope) -> None: ...
    async def aclose(self) -> None: ...


class ScopedMemoryStore:
    def __init__(
        self,
        store: MemoryStore,
        *,
        scope: MemoryScope,
        cache: MemoryPrefetchCache,
        store_identity: str,
    ) -> None: ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]: ...

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None: ...

    def prefetch_search(
        self,
        query: str,
        *,
        limit: int,
        namespace: str,
        metadata_filter: dict[str, Any] | None,
        owner: Session,
    ) -> _MemoryPrefetchRead: ...
```

`_MemoryPrefetchRead` is module-private. It carries detached results or
`None`, plus one of `fresh`, `stale`, `miss`, or `saturated`; only
`MemoryContextBuilder` consumes it. It is not a new event or a protocol method
that existing `MemoryStore` implementations must add.

`MemoryScope` rejects blank `tenant_id` or `namespace`. A scope is supplied by
trusted authentication/authorization code, never derived from a model tool
argument, prompt, query, or memory item. `store_identity` is a required,
opaque, bounded non-secret label chosen by the host; it must not be a URL,
credential, or connection string. Requiring it prevents accidental cache
sharing when two stores have otherwise equal scopes and queries.

`MemoryPrefetchConfig` rejects negative TTLs and non-positive entry, byte, or
in-flight bounds. Its defaults cap the retained cache at 128 entries, a 1 MiB
logical byte budget, and eight live refreshes. That byte budget charges the
canonical full key as well as retained result data; it is not a cap on the
caller input, an in-flight store response, remote payloads, or whole-process
heap use. A host can set `fresh_ttl_s=0` to skip fresh reuse while retaining
bounded stale refresh behavior.

`ScopedMemoryStore` is a duck-typed `MemoryStore` wrapper: existing tools,
hooks, and adapters can call its normal `search` and `upsert` methods without
changing the `MemoryStore` protocol. Its extra `prefetch_search` method is an
optional builder-only extension.

## 3. Builder wiring and unchanged default

Extend `MemoryContextBuilder.__init__` with:

```python
metadata_filter: dict[str, Any] | None = None,
prefetch: bool = False,
```

When `prefetch=False` (the default), it makes exactly the current awaited
`store.search(query, limit=..., namespace=...)` call, now forwarding the
optional `metadata_filter`. No cache object, task, changed latency, or
additional metadata is created.

When `prefetch=True`, the resolved store must expose `prefetch_search` and
the builder's non-empty `namespace` must equal the wrapped scope namespace.
Otherwise `build()` raises `ConfigError` before issuing a lookup. The builder
passes the current `turn.session` as owner and never falls back to a broad
`namespace=None` lookup. It formats returned results through the existing
formatters and remains request-only; a miss or saturation returns the normal
empty `ContextBuildResult` shape.

The existing `metadata["memory"]` keeps its current fields. The opt-in path
adds only `cache: "fresh" | "stale" | "miss" | "saturated"`; it adds no
tenant id, policy id/revision, store identity, cache key, query copy, raw
filter, error text, or result content to telemetry.

Example host wiring:

```python
scope = MemoryScope(
    tenant_id=authenticated_tenant_id,
    namespace=tenant_memory_namespace,
    policy_id="memory-read-policy",
    policy_revision=authorization_revision,
)
cache = MemoryPrefetchCache()
memory = ScopedMemoryStore(
    raw_memory_store,
    scope=scope,
    cache=cache,
    store_identity="primary-memory-v1",
)
builder = MemoryContextBuilder(
    memory,
    namespace=scope.namespace,
    prefetch=True,
    max_tokens=800,
)
```

The host, not Linch, remains responsible for obtaining all four trusted values
and for placing this wrapped store in `Agent.deps`/`RunOptions.deps`.

## 4. Scope enforcement and cache key

`ScopedMemoryStore` is a fail-closed boundary:

1. `search` and `prefetch_search` require `namespace == scope.namespace`.
   `None`, an empty value, and any alternate namespace raise
   `PermissionDeniedError`; the wrapper never converts them into an
   unscoped query.
2. `upsert` requires every item to have exactly `scope.namespace` before it
   reaches the wrapped store.
3. Results returned by the wrapped store must each carry that same namespace.
   A mismatched or missing namespace raises `PermissionDeniedError` and is
   neither returned nor cached.
4. Existing `MemorySearchTool`/`MemoryUpsertTool` may still accept a raw
   `namespace` field for backward compatibility, but a tool given this wrapper
   cannot bypass the enforced scope. A raw, unwrapped `MemoryStore` remains an
   application-defined storage primitive, not a tenancy or ACL mechanism.

The cache key is exactly:

```text
(
  scope.tenant_id,
  scope.namespace,
  scope.policy_id,
  scope.policy_revision,
  canonical_request(query, metadata_filter, limit),
  store_identity,
  generation(scope.family),
)
```

`scope.family` is `(tenant_id, namespace, policy_id)`. Query text is retained
byte-for-byte after the builder's existing query selection; do not lower-case,
trim again, or otherwise change retrieval semantics. `metadata_filter` must
be strict JSON data (finite numbers, string keys, no cycles), canonicalized
with sorted mapping keys and preserved array order. Invalid filter data raises
`ConfigError` rather than using `repr()` or a lossy cache key.

No key is shared across scope, policy revision, query/filter/limit, store
identity, or generation. A policy/authorization change must first call
`cache.invalidate(old_scope)`, then install a scope with the new policy
revision (and a new dependency binding if applicable). Thus a newly authorized
or revoked request cannot read an entry from the prior authorization identity;
the cache never uses a cross-scope fallback.

## 5. SWR, bounds, invalidation, and task ownership

Entries use a monotonic-clock timestamp and an LRU order. The retained-byte
charge is the UTF-8 byte length of the strict canonical full key (scope and
policy revision, query, filter, limit, store identity, and generation) plus
the retained result identifiers, namespaces, content, and strict-JSON metadata.
It deliberately describes only retained cache state, not caller, task, store,
remote-payload, or whole-process memory.

Before scheduling a refresh, calculate the canonical key charge. If it alone
exceeds `max_bytes`, return `saturated` with no result and do not schedule or
issue a store search, and do not log raw key material. This consumes neither a
cache entry nor an in-flight slot. `ScopedMemoryStore.search()` remains the
unchanged, authoritative awaited path. An unserializable result, or a completed
refresh whose key-plus-result charge exceeds `max_bytes`, is not retained and
does not evict healthy entries merely to try to fit an impossible candidate.
Otherwise remove expired entries, then evict
least-recently-used entries until both `max_entries` and the total full-entry
charge under `max_bytes` hold.

For an exact key:

- **Fresh** (`age <= fresh_ttl_s`): return a detached copy; do not refresh.
- **Stale** (`fresh_ttl_s < age <= fresh_ttl_s + stale_grace_s`): return a
  detached copy and start or join exactly one refresh for that key.
- **Miss/expired**: start one refresh if `max_inflight` permits, return no
  memory immediately, and do not await remote I/O.
- **Saturated**: return no fresh result on miss, or an already-valid stale
  result without scheduling new work. Do not queue an unbounded backlog.

Only the first caller schedules a key's refresh (single-flight). The task is
added to that caller session's existing `background_tasks` list and gets a
done callback that removes it from that list and clears the matching in-flight
slot. Therefore `Session.abort()`, `Session.aclose()`, and `Agent.close()` use
their existing cancel-and-await ordering; no new scheduler or lifecycle
registry is introduced. `MemoryPrefetchCache.aclose()` is idempotent and
cancels/awaits any remaining in-flight refreshes as a defensive host teardown.

`invalidate(scope)` is synchronous and idempotent: it increments the
per-family generation before removing matching entries and cancelling matching
refreshes. A refresh captures its generation before its first `await`; after
`store.search()` returns it may insert only when its captured generation still
equals the current family generation and it remains the key's current task.
Cancellation is not trusted as the fence: a late, cancellation-insensitive
result still fails this check. A failed refresh never stores an error or
replaces an existing stale value. `ScopedMemoryStore.upsert()` must call
`invalidate(scope)` immediately before it awaits the wrapped write and again
in a `finally` immediately after that await settles, whether it succeeds,
fails, or is cancelled. The first fence removes pre-write entries and
refreshes. A read while the write is pending may start and consume a refresh
of the pre-write store state; the final fence removes any such mid-write entry
and advances its generation. Thus no cache entry created during the pending
write may survive write completion, and a refresh that finishes after the
write cannot insert.

Out-of-band writes, erasure, credential rotation, or authorization changes
must use the same trusted-host bracket: `invalidate(scope)`, await the external
transition, then `invalidate(scope)` in `finally` on success, failure, or
cancellation. This is required to clear hits and fence refreshes that begin
while that external transition is pending.

A direct write to the wrapped underlying store, or a write by another process,
is not observed by this in-process cache. Without the trusted-host bracket (or
a future store-version hook), it has only the configured bounded stale
semantics, not write-completion correctness. A permission, revocation, or
scope transition must use that bracket and bind a new policy revision so the
next request is a hard miss, never an SWR authorization read.

The cache does not persist results, generations, in-flight work, or clocks.
Reload/resume starts empty and does not recreate background work.

## 6. Acceptance tests

All tests are offline and use controllable async fake stores. They must pin:

1. Default `MemoryContextBuilder` behavior remains awaited and cache-free;
   prefetch is opt-in and a cold prefetch returns no injected context while
   scheduling one refresh.
2. Equal queries in two tenant scopes never share a result. A missing,
   blank, mismatched, or `None` namespace under `ScopedMemoryStore` is denied;
   mismatched returned items are not exposed or cached.
3. Fresh reuse performs no second store read. A stale hit returns the old
   detached result immediately and concurrent stale readers start only one
   refresh. TTL expiry, LRU entry eviction, byte eviction, and in-flight
   saturation remain bounded. A huge valid query/filter whose canonical key
   alone exceeds the budget returns `saturated` without a task or store call;
   a key-plus-result candidate above the budget is not retained, and LRU
   accounting includes both key and result bytes.
4. Invalidation racing a paused refresh leaves no old result after the task
   finishes; a later lookup obtains only a post-fence refresh. A failed
   refresh neither becomes an entry nor overwrites a usable stale entry.
5. `ScopedMemoryStore.upsert()` fences immediately before and in `finally`
   after the delegated write. With a held write, a refresh may start and finish
   with old data while the write is pending, but releasing the write leaves a
   cache miss. A refresh held until after write completion cannot insert; the
   same final-fence miss holds when the write fails or is cancelled.
6. `MemorySearchTool`/`MemoryUpsertTool` using the wrapper cannot cross the
   configured scope even when their raw input asks for another namespace.
7. A session-owned paused refresh is cancelled and awaited by force release
   and by `Agent.close()` before dependent resources close; finished refresh
   tasks are removed from `session.background_tasks`.
8. Context metadata exposes only the bounded cache state. Retrieved memory
   remains ephemeral and does not enter `session.provider_view`, run
   checkpoints, or durable session history.
9. Public exports resolve through `linch.__all__`; no `MemoryStore` method,
   built-in store implementation, provider contract, or prompt changes are
   required.

## 7. Exact implementation surface

The implementation plan owns only these future changes:

- Create `src/linch/memory/prefetch.py` for `MemoryScope`, configuration,
  cache mechanics, `ScopedMemoryStore`, strict canonical cache-key helpers,
  and private task/result state.
- Modify `src/linch/memory/builder.py` for the explicit `prefetch` and
  `metadata_filter` arguments plus safe context metadata.
- Modify `src/linch/memory/__init__.py` and `src/linch/__init__.py` for the
  deliberate public exports.
- Modify `docs/usage/context-and-memory.md` with host-owned scope, invalidation,
  and cold-miss behavior; do not advertise namespace as an ACL by itself.
- Create `tests/memory/test_memory_prefetch.py`; extend
  `tests/storage/test_memory_primitives.py`, `tests/test_session_lifecycle.py`,
  and `tests/test_public_api.py` only for their named seams.

No implementation changes `MemoryStore`, any built-in storage backend,
provider adapter, prompt, compaction, session-log format, durable run format,
or OpenHuman/vendor checkout.

## 8. Open decisions for integration review

1. The defaults above intentionally favor low latency over first-turn recall.
   If a product requires a bounded wait for a cold lookup, it needs a separate
   explicit policy/API; do not silently make this SWR path await remote memory.
2. `store_identity` is deliberately host-supplied and opaque. If a future
   adapter needs automatic identity, add a separate documented duck-typed
   capability rather than deriving it from credentials, URLs, or `repr(store)`.
3. This wrapper protects calls routed through it. A multi-tenant host must
   still bind sessions to authorized stores/dependencies and invalidate or
   replace old bindings on authorization changes; no SDK cache can prove a
   host-provided store itself enforces tenant ACLs.
