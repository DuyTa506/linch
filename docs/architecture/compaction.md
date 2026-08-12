# Compaction

> Part of the [Linch architecture guide](./README.md).

`maybe_compact(session, agent)` is called at the top of each turn:

1. Count tokens in `provider_view` via `agent.provider.context_window(agent.model)`.
2. If within threshold — no-op.
3. Otherwise submit a summarization request via `agent.provider.stream()` and replace old messages in `provider_view` with the summary, emitting `CompactionEvent`.

**Invariant:** `full_history` is never modified. Only `provider_view` shrinks. Compaction uses the configured `agent.provider` — never a hardcoded OpenAI call.

The compaction path is provider-agnostic in Linch 2.0: summaries use the same
normalized provider stream contract as a normal turn, retain message/tool
pairing, and do not assume a coding workspace. Malformed or missing durable
snapshot state is treated as a cache miss and rebuilt from `full_history`.

```mermaid
flowchart TD
    A["Top of turn: maybe_compact()"] --> B{provider_view tokens<br/>within threshold?}
    B -- yes --> Z["no-op (byte-identical)"]
    B -- no --> C{compaction_ladder set?}
    C -- no --> S["summarize old messages<br/>via agent.provider.stream()"]
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
- **Rung 2 — forced compaction with a circuit breaker**: the reactive path
  (`_stream_turn_with_ladder` in `loop/streaming.py`) retries with forced compactions up
  to `max_forced_compactions` per run, then lets the error surface. The legacy
  path (no ladder) keeps its original single-retry-per-turn semantics in
  `_stream_turn_with_compaction_retry`.

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
in-memory, SQLite, and Postgres stores implement them; a custom store that omits
them keeps reconstructing the view from full history (byte-identical to before).
The snapshot payload is schema-versioned and read best-effort, so an older store
file stays forward-tolerant. Because the snapshot is only a cache, the load path
validates it and silently falls back to rebuilding from `full_history` whenever
it cannot be trusted — a loader error, a malformed payload, or a `covers_seq`
that is negative, ahead of the newest stored message, or inconsistent with a
non-monotonic message log. Message-sequence gaps are legal and preserved;
out-of-order sequences simply disable snapshot caching for that session.
Compaction never mutates or removes the append-only `full_history` — the
snapshot is a cache of a derived view, never a source of truth.

## Design rationale

- **Only `provider_view` shrinks; `full_history` is sacred.** The model only ever
  sees `provider_view`, so that is the only thing worth compacting. `full_history`
  stays complete because it is the durable audit record and the source of truth a
  resumed run rebuilds from — summarizing it would be lossy and irreversible.
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
