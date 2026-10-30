"""Cross-instance run creation remains idempotent under a write race."""

from __future__ import annotations

import asyncio
import threading

from linch.run_store import SqliteRunStore


async def test_sqlite_create_run_is_idempotent_across_instances(tmp_path) -> None:
    path = tmp_path / "runs.db"
    first = SqliteRunStore(path)
    second = SqliteRunStore(path)
    barrier = threading.Barrier(2, timeout=0.2)

    def trace(statement: str) -> None:
        normalized = statement.lstrip().lower()
        if normalized.startswith("select id, session_id, status"):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # With transaction serialization, the second SELECT is reached
                # only after the first creator commits and the barrier times out.
                pass

    try:
        # Open both executor connections before installing the trace hook so
        # schema initialization itself does not participate in the race.
        await first._exec.run(lambda conn: conn.set_trace_callback(trace))
        await second._exec.run(lambda conn: conn.set_trace_callback(trace))

        results = await asyncio.gather(
            first.create_run("session-1", id="shared-run"),
            second.create_run("session-1", id="shared-run"),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        assert errors == []
        records = [result for result in results if not isinstance(result, Exception)]
        assert len(records) == 2
        assert records[0].id == records[1].id == "shared-run"
        loaded = await first.load_run("shared-run")
        assert loaded is not None
    finally:
        await first.close()
        await second.close()
