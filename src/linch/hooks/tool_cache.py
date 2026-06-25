"""``ToolCacheHook`` — per-run memoization of idempotent (read-scope) tool calls.

On a repeated **exact-match** read-scope tool call within the same run, the hook
serves the prior result and short-circuits execution via the ``PreToolUse``
``resolve`` action — the tool is not re-run. Whenever a ``write``/``exec`` tool
runs, the run's cache is invalidated so a prior read is never served stale after
a mutation by the agent's own tools.

The cache is **per-run** (keyed by ``run_id``) and **in-memory**: lost on resume,
which is only a cache miss, so it is never checkpointed. This is the reason the
feature fits the hook model cleanly — unlike the loop guard, whose counters need
durable state.

This is an *efficiency* layer, not a *safety* one: it does not terminate runaway
loops (a cached call is cheap but the model can still ask forever). Keep the loop
guard as the backstop.

Known limitation: the per-run cache assumes the agent's own tools are the only
writers. A read whose underlying data is mutated by an *external* process between
two identical calls in the same run could be served stale.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..tools import ToolResult
from .contexts import AgentStopContext, PostToolUseContext, PreToolUseContext
from .types import HookResult

# Leak guard on the number of run buckets, in case ``on_agent_stop`` somehow
# never fires (e.g. a hard crash). Normal runs evict their bucket on that hook.
_MAX_RUNS = 64

# Distinguishes "absent" from a stored ``None`` value in writers.pop().
_MISSING: Any = object()


@dataclass(slots=True)
class ToolCacheConfig:
    """Configuration for :class:`ToolCacheHook`.

    Attributes:
        allow: If set, only these tool names are cacheable (still gated by
            read scope). ``None`` = every read-scope tool.
        deny: Tool names never cached, even if read-scope.
        max_entries: Per-run cap on cached entries (LRU eviction).
        max_value_bytes: Results whose content exceeds this are not cached. Large
            results are what the offload subsystem already handles; caching them
            would keep full payloads resident and re-offload on every served hit.
    """

    allow: set[str] | None = None
    deny: frozenset[str] = frozenset()
    max_entries: int = 256
    max_value_bytes: int = 100_000


@dataclass(slots=True)
class _RunCache:
    entries: OrderedDict[str, ToolResult] = field(default_factory=OrderedDict)
    # tool_use_id -> cache key, for a cacheable miss awaiting its result in post.
    # Bounded (LRU) because a backgrounded read never fires PostToolUse, so its
    # entry would otherwise never be popped.
    pending: OrderedDict[str, str] = field(default_factory=OrderedDict)
    # tool_use_ids of write/exec calls awaiting PostToolUse, where they clear the
    # cache *after* executing (so a read+write in the same turn invalidates too).
    writers: OrderedDict[str, None] = field(default_factory=OrderedDict)


class ToolCacheHook:
    """Opt-in per-run tool-result cache (see module docstring).

    Enable via ``Agent(tool_cache=ToolCacheConfig(allow={"Grep", "Search"}))``.
    It is **off by default**: caching only suits tools whose data is stable
    enough within a run (search/grep over a fixed corpus), so the host names
    which tools are cacheable rather than the SDK guessing.
    """

    name = "tool_cache"

    def __init__(self, config: ToolCacheConfig | None = None) -> None:
        self._cfg = config or ToolCacheConfig()
        self._runs: OrderedDict[str, _RunCache] = OrderedDict()

    async def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        scope = getattr(ctx.tool, "scope", None)
        if scope in ("write", "exec"):
            # Clear now to drop prior-turn reads before the write runs, and mark
            # the writer so on_post_tool_use clears AGAIN after it executes — the
            # PreToolUse pass runs over the whole turn before any tool executes,
            # so a read emitted before this write in the same turn is cached at
            # *its* PostToolUse, after this pre-clear; the post-clear catches it.
            # (The pre-clear is also the only invalidation a backgrounded write
            # gets, since the background path never dispatches PostToolUse.)
            bucket = self._bucket(ctx.run_id)
            bucket.entries.clear()
            _bounded_set(bucket.writers, ctx.tool_use_id, None, self._cfg.max_entries)
            return None
        if not self._cacheable(ctx.tool_name, scope):
            return None
        key = _cache_key(ctx.tool_name, ctx.input)
        if key is None:
            return None
        bucket = self._bucket(ctx.run_id)
        hit = bucket.entries.get(key)
        if hit is not None:
            bucket.entries.move_to_end(key)
            return HookResult.resolve(tool_result=hit).with_events(
                [_cache_event("cache_hit", ctx.tool_name)]
            )
        # Miss on a cacheable call: record so on_post_tool_use stores the result.
        _bounded_set(bucket.pending, ctx.tool_use_id, key, self._cfg.max_entries)
        return None

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        bucket = self._runs.get(ctx.run_id)
        if bucket is None:
            return None
        if bucket.writers.pop(ctx.tool_use_id, _MISSING) is not _MISSING:
            # A write/exec just finished executing — invalidate cached reads now,
            # which catches a read cached earlier in this same turn.
            bucket.entries.clear()
            return None
        key = bucket.pending.pop(ctx.tool_use_id, None)
        if key is None:
            return None
        result = ctx.result
        # Never cache errors — let the model retry a failed call.
        if result is None or getattr(result, "is_error", False):
            return None
        # Don't cache large results: offload handles those, and caching them
        # would pin full payloads in memory and re-offload on each served hit.
        if _result_size(result) > self._cfg.max_value_bytes:
            return None
        _bounded_set(bucket.entries, key, result, self._cfg.max_entries)
        return None

    async def on_agent_stop(self, ctx: AgentStopContext) -> HookResult | None:
        # A run ended (success OR error/abort/budget — on_agent_stop fires in the
        # loop's finally on every terminal path) — drop its cache so an error
        # termination doesn't orphan the bucket. True per-run lifetime.
        self._runs.pop(ctx.run_id, None)
        return None

    def _bucket(self, run_id: str) -> _RunCache:
        bucket = self._runs.get(run_id)
        if bucket is None:
            bucket = _RunCache()
            self._runs[run_id] = bucket
            while len(self._runs) > _MAX_RUNS:
                self._runs.popitem(last=False)
        else:
            self._runs.move_to_end(run_id)
        return bucket

    def _cacheable(self, tool_name: str, scope: Any) -> bool:
        if scope != "read":
            return False
        if tool_name in self._cfg.deny:
            return False
        if self._cfg.allow is not None and tool_name not in self._cfg.allow:
            return False
        return True


def _result_size(result: ToolResult) -> int:
    content = getattr(result, "content", "")
    return len(content) if isinstance(content, str) else len(str(content))


def _bounded_set(od: OrderedDict, key: Any, value: Any, cap: int) -> None:
    """Insert/refresh ``key`` at the MRU end, evicting the LRU entry past ``cap``."""
    od[key] = value
    od.move_to_end(key)
    while len(od) > cap:
        od.popitem(last=False)


def _cache_key(tool_name: str, input: dict[str, Any]) -> str | None:
    try:
        payload = json.dumps(input, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return f"{tool_name}\x00{payload}"


def _cache_event(kind: str, tool_name: str) -> Any:
    from ..events import HookEventRecord

    return HookEventRecord(event="PreToolUse", hook="tool_cache", action=kind, reason=tool_name)
