"""MemoryExtractionHook — the terminal-turn memory lifecycle seam (ROADMAP 3.1).

On a successful terminal turn this hook runs a caller-supplied *extractor* over
the ``full_history`` tail, dedups the candidates against what is already stored,
and upserts the survivors — then optionally triggers a gated consolidation pass.
It never alters the run's answer (always returns ``None``); a raising extractor
is isolated by the dispatcher, so a memory failure can't crash a run.

The extractor (the LLM side-query, the prompt, the definition of "a memory") is
embedder policy. This hook is pure wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from ..memory.lifecycle import ConsolidationGate, MemoryExtractionContext
from .contexts import StopContext
from .types import HookResult


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class MemoryExtractionHook:
    name = "memory_extraction"

    def __init__(
        self,
        store: Any,
        extractor: Any,
        *,
        tail_messages: int = 8,
        dedup: bool = True,
        dedup_threshold: float = 0.9,
        memory_write_tools: Iterable[str] = ("UpsertMemory",),
        consolidator: Any = None,
        consolidation_gate: ConsolidationGate | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.tail_messages = max(0, int(tail_messages))
        self.dedup = bool(dedup)
        self.dedup_threshold = float(dedup_threshold)
        self._write_tools = frozenset(memory_write_tools)
        self.consolidator = consolidator
        # When a consolidator is supplied but no gate, default to "run whenever
        # at least one memory changed" — the simplest useful policy.
        if consolidator is not None and consolidation_gate is None:
            consolidation_gate = ConsolidationGate(min_changes=1)
        self.consolidation_gate = consolidation_gate

    async def on_stop(self, ctx: StopContext) -> HookResult | None:
        if getattr(ctx.result_event, "subtype", None) != "success":
            return None
        session = ctx.session
        history = list(getattr(session, "full_history", []) or [])
        tail = history[-self.tail_messages :] if self.tail_messages else history
        # Don't step on the agent: if it explicitly wrote memory this turn, the
        # auto-extraction (and its consolidation) sits this one out.
        if self._wrote_memory(tail):
            return None

        ectx = MemoryExtractionContext(
            session=session,
            run_id=ctx.run_id,
            turn_index=ctx.turn_index,
            history=tail,
            store=self.store,
        )
        candidates = await _maybe_await(self.extractor(ectx))
        fresh: list[Any] = []
        for item in candidates or []:
            if self.dedup and await self._is_duplicate(item):
                continue
            fresh.append(item)

        if fresh:
            await self.store.upsert(fresh)
            if self.consolidation_gate is not None:
                self.consolidation_gate.record(len(fresh))

        if self.consolidator is not None and self.consolidation_gate is not None:
            await self.consolidation_gate.run(lambda: self.consolidator(self.store, ectx))

        return None

    def _wrote_memory(self, messages: list[Any]) -> bool:
        for message in messages:
            if getattr(message, "role", None) != "assistant":
                continue
            for block in getattr(message, "content", None) or []:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) in self._write_tools
                ):
                    return True
        return False

    async def _is_duplicate(self, item: Any) -> bool:
        query = getattr(item, "content", "") or ""
        if not query.strip():
            return False
        results = await self.store.search(
            query,
            limit=1,
            namespace=getattr(item, "namespace", None),
        )
        if not results:
            return False
        score = results[0].score
        return score is not None and score >= self.dedup_threshold
