# Scoped bounded SWR memory prefetch implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, tenant-scoped, bounded SWR memory wrapper that keeps
automatic retrieval off the provider-call critical path without weakening
memory scope or session-close guarantees.

**Architecture:** A host explicitly creates `MemoryPrefetchCache` and binds an
existing duck-typed `MemoryStore` to trusted `MemoryScope` through
`ScopedMemoryStore`. The wrapper fences/cache-invalidates its own writes and
offers a nonblocking `prefetch_search` extension; only
`MemoryContextBuilder(prefetch=True)` uses that extension. Refresh tasks are
owned by the caller `Session` through its existing `background_tasks`
lifecycle, and cache state is in-memory only.

**Tech Stack:** Python 3.10+, `asyncio`, standard-library `OrderedDict`,
monotonic clock and JSON, existing Linch memory/context/session seams, pytest.

**Spec:** `../specs/2026-10-30-memory-prefetch.md`

## Global Constraints

- Keep `MemoryContextBuilder(..., prefetch=False)` byte/behavior compatible:
  it awaits its ordinary search and creates no cache task.
- Do not add a mandatory method to `MemoryStore`, alter built-in stores,
  prompts, provider paths, compaction, or durable session/run state.
- Scope and `store_identity` are trusted host input, never model-controlled.
  The wrapper must deny absent, blank, or mismatched namespaces/results.
- Key exactly by scope/policy revision, canonical query/filter/limit, store
  identity, and generation; no process-global cache or cross-scope fallback.
- Use the exact default bounds from the spec: 5 s fresh TTL, 30 s stale grace,
  128 entries, 1,048,576 bytes, and 8 in-flight refreshes.
- Invalidation fences old refreshes even when cancellation is ignored. Wrapper
  writes invalidate before delegation; direct underlying/external writes require
  an explicit trusted-host `cache.invalidate(scope)` call.
- An authorization/scope change is a hard miss: invalidate the old scope before
  changing it and install a new policy revision. Never serve stale data as
  authorization evidence.
- Reuse existing `Session.background_tasks`; close/release must cancel and
  await refreshes. Finished tasks remove themselves from that list.
- This plan prepares a feature slice only. No staging, commit, publish, or
  release upload occurs during implementation work.

## Review Focus

1. Authorization rotation cannot reuse an old policy revision after
   invalidation — Task 1 race test.
2. Equal tenant queries cannot cross-hit — Task 1 scope-key test.
3. A cancellation-insensitive refresh cannot reinsert after its fence changes
   — Task 1 paused-refresh test.
4. Session/agent close awaits the task finalizer before dependent resources
   close — Task 3 lifecycle test.
5. Default/direct builders and arbitrary existing duck stores remain uncached
   and unrestricted — Task 2 compatibility test.

---

### Task 1: Scoped cache core and wrapper

**Files:**

- Create: `src/linch/memory/prefetch.py`
- Create: `tests/memory/test_memory_prefetch.py`

**Interfaces:**

- Consumes: unchanged `MemoryStore.search/upsert`, `MemoryItem`,
  `MemorySearchResult`, `PermissionDeniedError`, and a caller session with
  `background_tasks`.
- Produces: public `MemoryScope`, `MemoryPrefetchConfig`,
  `MemoryPrefetchCache`, and `ScopedMemoryStore`; private
  `_MemoryPrefetchRead` consumed by Task 2.

- [ ] **Step 1: Write focused failing cache tests**

  Add controllable async fake stores and tests named:

  - `test_scope_requires_nonempty_tenant_and_namespace`
  - `test_equal_queries_in_distinct_scopes_never_share_cache_entries`
  - `test_scoped_store_rejects_none_mismatched_and_leaked_namespaces`
  - `test_stale_reads_start_one_refresh_and_return_detached_old_results`
  - `test_lru_byte_entry_and_inflight_bounds_are_enforced`
  - `test_invalidate_fences_a_paused_refresh_from_reinsertion`
  - `test_failed_refresh_does_not_poison_or_replace_stale_entry`
  - `test_scoped_upsert_fences_before_delegating_write`

  Drive TTL through a private injected/test clock or monkeypatched module
  monotonic clock; do not sleep for TTLs. Hold `store.search()` on an event in
  the race test, invalidate, release it, then assert only a new-generation
  lookup can repopulate the cache.

- [ ] **Step 2: Run the focused tests to prove the API is absent**

  Run: `pytest tests/memory/test_memory_prefetch.py -q`

  Expected: collection/import failure for the new prefetch types.

- [ ] **Step 3: Implement the narrow cache in `src/linch/memory/prefetch.py`**

  Implement the exact spec signatures. Keep cache state per explicit instance;
  use strict JSON canonicalization for filters, LRU entry/byte eviction,
  per-key single-flight, and a family generation captured before the search
  await. `invalidate(scope)` purges/cancels matching state and increments the
  generation; insertion requires both generation equality and current-task
  identity. Never log a key, query, filter, result, scope, or exception text.

  `ScopedMemoryStore.search()` remains authoritative and awaited. Its
  `prefetch_search()` returns fresh/stale/miss/saturated without awaiting I/O,
  schedules only the first permitted task, validates request/result namespace,
  and tracks that task in the owner session. `upsert()` fences before calling
  the wrapped store. `aclose()` is idempotent and cancels/awaits only cache
  work; it never closes the wrapped store.

- [ ] **Step 4: Compile, test, and request fresh review of this slice**

  Run:

  ```text
  pytest tests/memory/test_memory_prefetch.py -q
  ruff check src/linch/memory/prefetch.py tests/memory/test_memory_prefetch.py
  ruff format --check src/linch/memory/prefetch.py tests/memory/test_memory_prefetch.py
  pyright
  ```

  Expected: PASS. Give a fresh reviewer the spec sections 2, 4, and 5 plus
  this task's diff before proceeding.

### Task 2: Explicit context-builder integration

**Files:**

- Modify: `src/linch/memory/builder.py`
- Modify: `tests/storage/test_memory_primitives.py`

**Interfaces:**

- Consumes: Task 1's optional `prefetch_search` extension; existing context
  formatters and `ContextBudget`.
- Produces: `MemoryContextBuilder(..., metadata_filter=None, prefetch=False)`
  with the direct path unchanged and a safe, opt-in prefetch path.

- [ ] **Step 1: Write failing builder integration tests**

  Add tests named:

  - `test_default_memory_context_builder_still_awaits_direct_search`
  - `test_prefetch_builder_cold_miss_then_fresh_ephemeral_context`
  - `test_prefetch_builder_reports_only_safe_cache_state_metadata`
  - `test_prefetch_builder_requires_scoped_prefetch_store_and_exact_namespace`
  - `test_prefetch_context_is_not_persisted_or_compacted`

  Assert a cold request emits no retrieved-memory message and does not wait for
  a held store. After the task settles, assert the next request injects the
  existing formatted result. Assert only the safe cache-state addition appears
  in metadata and the injection remains outside `session.provider_view`.

- [ ] **Step 2: Run the focused tests to verify failure**

  Run: `pytest tests/storage/test_memory_primitives.py -k 'prefetch or default_memory_context' -q`

  Expected: FAIL because builder options and the nonblocking branch are absent.

- [ ] **Step 3: Extend `MemoryContextBuilder` minimally**

  Add only the keyword parameters in the spec. The direct path forwards
  `metadata_filter` to ordinary `search` and does not create a cache. The
  opt-in path requires a callable `prefetch_search` and an exact nonempty
  namespace, passes `turn.session` as owner, maps its private status into safe
  existing memory metadata, and retains existing formatting/budget semantics.

- [ ] **Step 4: Compile, test, and request fresh review of this slice**

  Run:

  ```text
  pytest tests/storage/test_memory_primitives.py tests/context/test_context_builder.py -q
  ruff check src/linch/memory/builder.py tests/storage/test_memory_primitives.py
  ruff format --check src/linch/memory/builder.py tests/storage/test_memory_primitives.py
  pyright
  ```

  Expected: PASS. Give a fresh reviewer the spec sections 1, 3, and 6 plus
  this task's diff before proceeding.

### Task 3: Lifecycle, tool-boundary, and public API proof

**Files:**

- Modify: `src/linch/memory/__init__.py`
- Modify: `src/linch/__init__.py`
- Modify: `tests/test_session_lifecycle.py`
- Modify: `tests/test_public_api.py`
- Modify: `tests/memory/test_memory_prefetch.py`

**Interfaces:**

- Consumes: Task 1 public types and Task 2 builder behavior.
- Produces: intentional top-level imports and proof that current session/agent
  teardown owns all refresh tasks.

- [ ] **Step 1: Add lifecycle and public-surface failing tests**

  In `tests/test_session_lifecycle.py`, add
  `test_force_release_cancels_and_awaits_scoped_memory_refresh`: start a held
  refresh through a real session, force-release it, and assert its finalizer
  ran and the task is no longer retained in `background_tasks`. Reuse the
  existing provider-close ordering fixture to pin `Agent.close()` ordering.

  In `tests/memory/test_memory_prefetch.py`, exercise `MemorySearchTool` and
  `MemoryUpsertTool` against a `ScopedMemoryStore` with a raw alternate
  namespace and assert the wrapper denies it. In `tests/test_public_api.py`,
  assert `MemoryScope`, `MemoryPrefetchConfig`, `MemoryPrefetchCache`, and
  `ScopedMemoryStore` resolve from `linch` and are the same objects exported
  by `linch.memory`; private cache entry/read types remain unexported.

- [ ] **Step 2: Run the focused tests to verify missing lifecycle/export wiring**

  Run: `pytest tests/test_session_lifecycle.py -k memory_refresh -q && pytest tests/test_public_api.py -q`

  Expected: FAIL until Task 1 types are promoted and task ownership is wired.

- [ ] **Step 3: Wire exports only; rely on existing lifecycle cancellation**

  Add only the four intended public types to `linch.memory.__all__`, the root
  lazy export map/import branch, and root `__all__`. Do not add a new Session
  task registry or change `MemoryStore`; Task 1's done callback and the
  existing `background_tasks` drain are the lifecycle integration.

- [ ] **Step 4: Compile, test, and request fresh review of this slice**

  Run:

  ```text
  pytest tests/memory/test_memory_prefetch.py tests/test_session_lifecycle.py tests/test_public_api.py -q
  ruff check src/linch/memory src/linch/__init__.py tests/memory/test_memory_prefetch.py tests/test_session_lifecycle.py tests/test_public_api.py
  ruff format --check src/linch/memory src/linch/__init__.py tests/memory/test_memory_prefetch.py tests/test_session_lifecycle.py tests/test_public_api.py
  pyright
  ```

  Expected: PASS. Give a fresh reviewer the spec sections 2, 4, and 5 plus
  this task's diff before proceeding.

### Task 4: Documentation and prerelease preparation

**Files:**

- Modify: `docs/usage/context-and-memory.md`
- Verify: `pyproject.toml`
- Test: `tests/test_vector_memory_adapter_docs.py`

**Interfaces:**

- Consumes: final public names and exact host contract from Tasks 1–3.
- Produces: accurate host guidance and release-ready version/change evidence;
  it does not publish or choose a release independently.

- [ ] **Step 1: Document the explicit host wiring and non-goals**

  Add a compact example using `MemoryScope`, `MemoryPrefetchCache`,
  `ScopedMemoryStore`, and `MemoryContextBuilder(prefetch=True)`. State that
  cold/expired/saturated recall omits memory instead of blocking, ordinary
  builders stay awaited, caches are non-durable, scope is authenticated-host
  input, and namespace alone is not an ACL. State the hard operational rule:
  wrapper writes self-invalidate; direct raw-store/external writes,
  erase/credential rotation, and authorization changes require trusted-host
  invalidation before the transition and a new policy revision/binding.

- [ ] **Step 2: Prepare, but do not publish, the prerelease handoff**

  Verify the current project version in `pyproject.toml`. The integration owner
  selects the next allowed prerelease/version and release-note location only
  after all coordinated feature slices, tests, and reviews pass. Record the
  chosen version and memory-prefetch behavior in that integration handoff; do
  not change a version, tag, build, upload, or invoke a package publisher in
  this feature worker.

- [ ] **Step 3: Run final documentation and regression verification, then fresh review**

  Run:

  ```text
  pytest tests/test_vector_memory_adapter_docs.py tests/memory/test_memory_prefetch.py tests/storage/test_memory_primitives.py tests/test_session_lifecycle.py tests/test_public_api.py -q
  ruff check src tests
  ruff format --check src tests
  pyright
  pytest
  ```

  Expected: PASS. Give a fresh reviewer the complete spec, all four task diffs,
  and the no-publish prerelease note before integration.

## Spec coverage self-review

- Explicit opt-in/default compatibility: Task 2.
- Trusted scope and exact namespace enforcement: Task 1 and Task 3 tool test.
- Key components, strict filter canonicalization, bounded LRU/TTL/in-flight:
  Task 1.
- Generation fencing, wrapper-write and explicit-host invalidation, and hard
  authorization misses: Task 1 plus Task 4 documentation.
- Session-owned cancellation and agent-close ordering: Task 3.
- Ephemeral context/no prompt or durable-state change: Task 2.
- Intentional public API, docs, version/prerelease handoff, and no publish:
  Tasks 3–4.

The only intentionally unresolved product choice is a future, separately
specified bounded wait for cold recall. This plan does not add one.
