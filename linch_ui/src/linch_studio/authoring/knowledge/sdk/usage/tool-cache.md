# Tool cache

`ToolCacheHook` memoizes **read-scope** tool calls **within a single run** so the
model doesn't pay to re-run an identical lookup it already made. It is an
*efficiency* layer built on the hook system — not a safety one (it does not stop
runaway loops; the [loop guard](./agent.md) remains the backstop).

It is **off by default**. Caching only suits tools whose data is stable enough
within a run (search/grep over a fixed corpus), so you name which tools are
cacheable rather than the SDK guessing.

## Enable

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
@dataclass
class ToolCacheConfig:
    allow: set[str] | None = None   # None = every read-scope tool
    deny: frozenset[str] = frozenset()
    max_entries: int = 256          # per-run LRU cap
```

## How it works

The hook sits at two chokepoints (see [hooks.md](./hooks.md)):

- **`PreToolUse`** — on an **exact-input** match for a cacheable tool, it returns
  `HookResult.resolve(tool_result=...)`: the tool is **not executed** and the
  cached result is served.
- **`PostToolUse`** — a successful result for a cacheable miss is stored.

The cache key is `tool_name` + the canonical JSON of the input, so only
**identical** calls hit (`{"query": "a"}` and `{"query": "b"}` never collide).

## Correctness guarantees

- **Read scope only.** `write`/`exec` tools are never cached, even if you put
  them in `allow` — re-running a write can be intentional, and serving a stale
  result for a side-effecting tool would be a bug.
- **Writes invalidate reads.** When any `write`/`exec` tool runs, the run's
  cached reads are cleared, so a `Read → Write → Read` of the same data
  re-executes the second read instead of serving the pre-write value.
- **Errors are never cached** — a failed call can still be retried.
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
