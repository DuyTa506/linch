"""Shared helpers for durable run fixtures."""

from __future__ import annotations

from typing import Any


async def pin_run_contract(session: Any, run_id: str, opts: Any = None) -> None:
    """Attach the current execution contract to an in-memory run record.

    Args:
        session: Session whose agent and run store own the run.
        run_id: Identifier of the hand-built run record to update.
        opts: Optional run options used to build the contract.
    """

    from linch import RunOptions
    from linch.loop.contracts import _build_run_contract
    from linch.run_store import run_meta_with_contract

    await session.agent._ensure_provider_prepared()
    record = session.agent.run_store._runs[run_id]
    assert "run_contract" not in record.meta
    record.meta = run_meta_with_contract(
        _build_run_contract(session, record.checkpoint.prompt, opts or RunOptions()), record.meta
    )
