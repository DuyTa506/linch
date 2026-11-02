# Tool cache

Use `ToolCacheConfig` to memoize read tool calls whose data stays stable within a run.

`ToolCacheHook` memoizes **read-scope** tool calls **within a single run** so the
model doesn't pay to re-run an identical lookup it already made. It is an
*efficiency* layer built on the hook system — not a safety one (it does not stop
runaway loops; the [loop guard](./agent.md) remains the backstop).

It is **off by default**. Caching only suits tools whose data is stable enough
within a run (search/grep over a fixed corpus), so you name which tools are
cacheable rather than the SDK guessing.

<a id="enable"></a>

## `ToolCacheConfig`

```python
from linch import Agent, ToolCacheConfig

# Selective (recommended): only these tools are cached.
agent = Agent(..., tool_cache=ToolCacheConfig(allow={"Grep", "Search"}))

# Every read-scope tool (you accept the staleness trade-off below).
agent = Agent(..., tool_cache=True)

# Default: disabled.
agent = Agent(...)
```

`tool_cache` accepts a `ToolCacheConfig`, a ready `ToolCacheHook`, `True`
(all read-scope), or `None`/`False` (off).

```python
from dataclasses import dataclass


@dataclass(slots=True)
class ToolCacheConfig:
    allow: set[str] | None = None   # None = every read-scope tool
    deny: frozenset[str] = frozenset()
    max_entries: int = 256          # per-run LRU cap
    max_value_bytes: int = 100_000   # maximum cached result size
```

`ToolCacheHook` requires `max_entries >= 1` and `max_value_bytes >= 0`.
Results larger than `max_value_bytes` are not cached.

## How it works

The hook sits at two chokepoints (see [hooks.md](./hooks.md)):

- **`PreToolUse`** — on an **exact-input** match for a cacheable tool, it returns
  `HookResult.resolve(tool_result=...)`: the tool is **not executed** and the
  cached result is served.
- **`PostToolUse`** — a successful result for a cacheable miss is stored.

The cache key is `tool_name` + the sorted JSON of the input seen by the cache hook, so only
**identical** calls hit (`{"query": "a"}` and `{"query": "b"}` never collide).

## Correctness guarantees

- **Read scope only.** `write`/`exec` tools are never cached, even if you put
  them in `allow` — re-running a write can be intentional, and serving a stale
  result for a side-effecting tool would be a bug.
- **Writes invalidate reads.** When any `write`/`exec` tool runs, the run's
  cached reads are cleared before dispatch and again at completion, including
  background completion through `PostToolUse`. A `Read → Write → Read` after the
  write completes re-executes the second read. Concurrent reads can still see
  stale values until an in-flight write completes.
- **Errors are never cached.** Automatic retries require `RetryOptions`;
  caching does not enable retries.
- **Per-run.** The cache is keyed by `run_id` and dropped when the run ends. It
  is in-memory and **not checkpointed**: after a durable resume it is simply
  cold (a cache miss), never wrong.

## Caveat

The per-run cache assumes the agent's own tools are the only writers. A read
whose underlying data is mutated by an **external** process between two identical
calls in the same run could be served stale. Keep cacheable tools to data that is
stable for the duration of a run, or leave the cache off for volatile sources.

## Related

- [Hooks](./hooks.md) — the chokepoints and the `resolve` action this builds on.
- [Tools](./tools.md) — tool `scope`, which decides cacheability.
