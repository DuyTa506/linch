# Compaction

> Part of the [Linch architecture guide](./README.md).

`maybe_compact(session, agent)` is called at the top of each turn:

1. Count tokens in `provider_view` via `agent.provider.context_window(agent.model)`.
2. If within threshold — no-op.
3. Otherwise submit a summarization request through the run's accounted
   auxiliary gateway (which uses `agent.provider`), settle its physical
   delivery receipt, and record the summary as a **logged projection** —
   `session.session_log.record_projection(...)` appends a `ProjectionEntry`
   and rewrites the derived `provider_view` — emitting `CompactionEvent`.

**Invariant:** `full_history` is never modified. Only `provider_view` (a projection of the single append-only log) shrinks, and it shrinks by a *logged* projection, not an untracked in-place mutation. This covers session-owned model input; per-request `ContextInjectionHook`/RAG context is ephemeral and exists only in `ProviderRequest`. Compaction uses the configured `agent.provider` — never a hardcoded OpenAI call.

The compaction path is provider-agnostic: summaries use the same
normalized provider stream contract as a normal turn, retain message/tool
pairing, and do not assume a coding workspace. Missing or malformed current
wire-3 snapshot caches can be rebuilt from `full_history`; incompatible snapshot
versions are rejected rather than treated as cache misses.

Compaction never changes the stored `system` or `tools`. When `notified` differs
from the immutable `base`, it resets `notified` to a copy of `base`, persists that
metadata, and clears the catalog fast path. The next synchronization emits one
consolidated reminder of the live catalog's differences from `base`, retaining
existing wire tool rows. With no changes, compaction performs no catalog metadata
write. A slow-path synchronization also performs the reset when `notified`
differs from `base` but the provider view has no catalog reminder, recovering a
crash between projection persistence and metadata reset. Opt-in positional adapters
suppress duplicate native additions if an earlier block survives alongside the
consolidated notice. Frozen definitions and historical native provenance remain in
the audit log for durable contract/schema comparisons even when compaction drops
them from the provider view. An Anthropic by-value removal stays text-only while
its provenance is in that view; a promoted prefix name can use a declared reference
once its original definition is absent.

Skill listing fingerprints and compaction context come from the session's frozen
prefix when snapshots are enabled for the run. Reloading skills therefore does
not independently change a durable run's compaction contract. Runs that bypass
snapshots continue to reinject their live listing. Reinjected listing and invoked
skill reminders are provider-view-only log entries (`historical=False`); they do
not add duplicate skill bodies to `full_history`.

The direct `summarize_with_provider(...)` helper remains available for
standalone callers with its existing provider-stream behavior. The normal run
path wraps built-in strategies and compliant custom strategies in the
accounting gateway so cancellation, one provider-slot acquisition, and receipt
settlement are shared with the primary loop.

```mermaid
flowchart TD
    A["Top of turn: maybe_compact()"] --> B{provider_view tokens<br/>within threshold?}
    B -- yes --> Z["no-op (byte-identical)"]
    B -- no --> C{compaction_ladder set?}
    C -- no --> S["accounted summary delivery<br/>via agent.provider"]
    C -- yes --> M["Rung 1: micro_compact<br/>elide old ToolResultBlocks<br/>(no LLM · copy-on-write)"]
    M --> D{enough freed?}
    D -- yes --> Z2["proceed"]
    D -- no --> S
    S --> E["replace old msgs in provider_view<br/>emit CompactionEvent"]
    E --> F["full_history untouched"]
    R(["ContextLengthError mid-turn"]) -. reactive .-> M
    R -. exceeds max_forced_compactions .-> X["error surfaces"]
```

`DefaultCompaction` remains the default. `DetailedCompaction` is opt-in via
`Agent(compaction=DetailedCompaction())` and uses a continuation-safe summary
with user intent, key information/artifacts touched, errors/fixes, pending tasks,
current work, and the next step. The section wording is domain-neutral (it asks
for identifiers — paths, URLs, IDs, names — rather than assuming files/code), so
it suits non-coding hosts too.

`DefaultCompaction`'s default prompt is coding-oriented (it asks for file paths,
tool calls, and the like); `DetailedCompaction`'s default is domain-neutral.
Since linch is *embeddable* and not every host is a coding agent, **both** accept
a `prompt=` override — `DefaultCompaction(prompt=...)` /
`DetailedCompaction(prompt=...)` — so a non-coding host can reword the summary
without reimplementing a `CompactionStrategy`. Omitting it keeps each strategy's
built-in prompt, so default behavior is unchanged. A ready-made domain-neutral prompt ships as
`GENERAL_SUMMARY_PROMPT` (public) for hosts that want a sensible non-coding
default without writing their own:
`Agent(compaction=DefaultCompaction(prompt=GENERAL_SUMMARY_PROMPT))`.

### Accounting and durable paid-result recovery

The run-scoped ledger is the source of cumulative model usage. A physical
compaction delivery gets a fresh delivery ID, and a valid `message_end` usage is
settled exactly once before the result can be accepted, rejected, applied to
the provider view, or followed by another dispatch. Valid usage remains billed
even when the summary is empty, non-text, tool-using, oversized, or otherwise
unusable. A summary that exhausts a `RunBudget` is followed by an admission
recheck, so the next primary request is not dispatched.

For `DefaultCompaction` and `DetailedCompaction`, a durable accepted result is
paired with its receipt and an ordered, hash-only `compaction_source` recipe.
The recipe binds the physical delivery, request, strategy configuration,
durable full-history boundary, ordered source messages, and provider-view-only
reminders. Required invoked-skill bodies are flushed through the existing
session store before the recipe is written. On resume, Linch reconstructs only
from that verified historical source and applies the bounded accepted text
without another provider call or duplicate charge. A changed source, missing
skill body, changed listing wrapper, mismatched strategy/configuration, or
legacy source-less paid record fails closed.

The custom `CompactionStrategy` protocol is unchanged. Calls made through the
supplied provider facade receive the run's accounting, cancellation, and
limiter guarantees. Opaque calls outside the facade and arbitrary transforms
are not observable, and custom strategies do not receive deterministic paid
replay unless they provide an explicit deterministic result/commit seam.

An unknown final usage is never treated as zero. Capped runs block subsequent
model calls at the unknown-use fence; uncapped runs may report a known
lower-bound total. Shared receipt ownership covers the live in-process budget
tree only — it does not promise distributed cap recovery, exactly-once
external billing, or reservations for concurrent admissions.

### Compaction ladder (opt-in)

`Agent(compaction_ladder=CompactionLadder())` adds recovery rungs around the
strategy; with the default `compaction_ladder=None` the behavior above is
byte-identical.

- **Rung 1 — micro-compact** (`micro_compact` in `compaction.py`): elide
  `ToolResultBlock` contents older than `keep_recent_turns` with a short
  placeholder. No LLM call; copy-on-write (blocks are shared with
  `full_history`, so changed messages are rebuilt, never mutated); every
  `tool_use_id` stays paired. Runs proactively inside `maybe_compact` (skips
  summarization when elision frees enough) and reactively, once per turn, when
  the provider raises `ContextLengthError`.
- **Rung 2 — forced compaction with a circuit breaker**: the shared
  `_stream_turn_with_recovery` driver in `loop/streaming.py` retries with forced
  compactions up to `max_forced_compactions` per run, then lets the error surface.
  Without a ladder, that same driver retains one compaction retry per turn.
  Consolidation preserves each policy's retry/fallback boundaries, observer
  ordering, and accounting.

### Durable compacted views (opt-in, store-detected)

Compaction shrinks `provider_view` in memory, but on a fresh process the view is
rebuilt from `full_history` — so a reloaded session would re-pay the compaction.
A `SessionStore` **may** persist the compacted view so a reload skips that work:

```python
class ProviderViewSnapshotStore(Protocol):
    async def save_provider_snapshot(self, id: str, snapshot: ProviderViewSnapshot) -> None: ...
    async def load_provider_snapshot(self, id: str) -> ProviderViewSnapshot | None: ...
```

`ProviderViewSnapshot` pairs the compacted `provider_view` with `covers_seq` —
the message sequence number it accounts for. The loop saves a snapshot at the
compaction seam; on load, the agent restores the snapshot and appends only the
messages written *after* `covers_seq`, reproducing the live view exactly.

Both methods are optional and detected at runtime with `getattr`. The built-in
in-memory and Postgres stores implement them; a custom store that omits
them keeps reconstructing the view from full history (byte-identical to before).
The snapshot envelope requires exact wire version 3. Missing, older, or unknown
versions fail with the start-new-run error; incompatible stores require fresh
databases. After the version gate, the load path validates the current snapshot
cache and falls back to rebuilding from `full_history` on an ordinary loader
error, malformed payload, or a `covers_seq` that is negative, ahead of the newest
stored message, or inconsistent with a non-monotonic message log. Message-sequence gaps are legal and preserved;
out-of-order sequences simply disable snapshot caching for that session.
Compaction never mutates or removes the append-only session log's historical
projection — the snapshot is a cache of a derived view, never a source of truth.

## Design rationale

- **Only the provider projection shrinks; historical messages stay complete.**
  The model only ever sees `provider_view`, so `record_projection(...)` rewrites
  that derived projection while leaving the `full_history` projection intact.
  The append-only `SessionLog` remains the source of truth; persisted session
  snapshots are the durable boundary, not a projection-entry journal.
- **Summarize through `agent.provider`, never a hardcoded vendor call.** Compaction
  is just another model turn, so it must honor the same provider the run uses.
  Hardcoding OpenAI would break Anthropic/Gemini/local users and split the cost
  accounting.
- **Cheapest rung first.** `micro_compact` is LLM-free (elide stale tool results)
  and runs before paying for a summarization call; summarization only fires when
  elision can't free enough. This keeps the common case free and fast.
- **Copy-on-write, not mutation.** `provider_view` blocks are shared by reference
  with `full_history`, so elision rebuilds the changed messages instead of mutating
  them — otherwise shrinking the model's view would silently corrupt the durable
  record. `tool_use_id` pairing is preserved so no provider rejects an orphaned result.
- **Circuit breaker over open-ended retry.** Forced compaction is capped at
  `max_forced_compactions` per run so a pathologically large single turn surfaces a
  clear `ContextLengthError` instead of looping on summarization forever.

---

Back to the [architecture index](./README.md).
