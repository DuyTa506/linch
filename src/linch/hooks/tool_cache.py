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
from .contexts import PostToolUseContext, PreToolUseContext, StopContext
from .types import HookResult

# Number of completed-but-not-evicted run buckets to retain as a leak guard in
# case a ``Stop`` event never fires (e.g. a hard crash). Far above any real
# concurrency; normal runs evict their bucket on ``on_stop``.
_MAX_RUNS = 64


@dataclass(slots=True)
class ToolCacheConfig:
    """Configuration for :class:`ToolCacheHook`.

    Attributes:
        allow: If set, only these tool names are cacheable (still gated by
            read scope). ``None`` = every read-scope tool.
        deny: Tool names never cached, even if read-scope.
        max_entries: Per-run cap on cached entries (LRU eviction).
    """

    allow: set[str] | None = None
    deny: frozenset[str] = frozenset()
    max_entries: int = 256


@dataclass(slots=True)
class _RunCache:
    entries: OrderedDict[str, ToolResult] = field(default_factory=OrderedDict)
    # tool_use_id -> cache key, for a cacheable miss awaiting its result in post.
    pending: dict[str, str] = field(default_factory=dict)


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
        # A mutating call invalidates the run's cached reads — they may now be
        # stale. Clear before it executes; this run's later reads re-cache fresh.
        if scope in ("write", "exec"):
            bucket = self._runs.get(ctx.run_id)
            if bucket is not None:
                bucket.entries.clear()
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
        bucket.pending[ctx.tool_use_id] = key
        return None

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        bucket = self._runs.get(ctx.run_id)
        if bucket is None:
            return None
        key = bucket.pending.pop(ctx.tool_use_id, None)
        if key is None:
            return None
        result = ctx.result
        # Never cache errors — let the model retry a failed call.
        if result is None or getattr(result, "is_error", False):
            return None
        bucket.entries[key] = result
        bucket.entries.move_to_end(key)
        while len(bucket.entries) > self._cfg.max_entries:
            bucket.entries.popitem(last=False)
        return None

    async def on_stop(self, ctx: StopContext) -> HookResult | None:
        # A run ended — drop its cache (true per-run lifetime).
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


def _cache_key(tool_name: str, input: dict[str, Any]) -> str | None:
    try:
        payload = json.dumps(input, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return f"{tool_name}\x00{payload}"


def _cache_event(kind: str, tool_name: str) -> Any:
    from ..events import HookEventRecord

    return HookEventRecord(event="PreToolUse", hook="tool_cache", action=kind, reason=tool_name)
