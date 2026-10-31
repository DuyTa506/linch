"""Small end-to-end smoke coverage for the multi-process load-test harness.

The smoke point is deliberately driven through the public command-line harness:
that exercises the same proxy, API/enqueuer, and worker subprocess topology as
the longer measurements.  It is skipped on developer machines without the
Postgres fixture, just like the other worker integration tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _report(stdout: str) -> dict[str, Any]:
    """Extract the harness's top-level JSON report from optional summary text."""

    decoder = json.JSONDecoder()
    for offset, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("experiment") == "quick":
            return value
    raise AssertionError(f"load_test.py did not emit a quick JSON report:\n{stdout}")


@pytest.mark.skipif(
    not os.getenv("AGENT_KIT_TEST_PG_DSN"),
    reason="AGENT_KIT_TEST_PG_DSN unavailable",
)
@pytest.mark.asyncio
async def test_load_harness_quick_topology_is_terminal_exactly_once():
    """Run a short M=2 topology point and verify durable terminal accounting."""

    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    # The harness keeps the quick point short when this flag is present.  It is
    # intentionally separate from its CLI so the smoke remains a few seconds
    # even if the normal ``--quick`` gate is tuned for a longer run.
    env["LINCH_LOAD_TEST_SMOKE"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(root / "scripts" / "load_test.py"),
        "--quick",
        cwd=root,
        env=env,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=90)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise

    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()
    assert proc.returncode == 0, stderr or stdout

    report = _report(stdout)
    assert report["experiment"] == "quick"
    points = report["points"]
    assert isinstance(points, list) and len(points) == 1
    point = points[0]
    assert point["workers"] == 2, point
    assert point["concurrency"] == 8, point
    assert point["rtt_ms"] == 1, point
    assert point["enqueued"] > 0, point
    assert point["non_terminal"] == 0, point
    assert point["completed"] == point["enqueued"], point
    assert point["failed"] == 0, point
    assert point["aborted"] == 0, point
    assert point["terminal_exactly_once"] is True, point
    assert point["duplicate_terminal"] == 0, point
    assert point["event_sequence_contiguous"] is True, point
    assert point["duplicate_save_notes"] == 0, point
    assert point["queue_remaining"] == 0, point
    assert point["stream"]["in_order"] is True, point


@pytest.mark.asyncio
async def test_proxy_delays_bursts_once_and_preserves_fragmented_bytes():
    """The relay queues reads by timestamp, avoiding per-fragment HOL delay."""

    from importlib.util import module_from_spec, spec_from_file_location

    root = Path(__file__).resolve().parents[2]
    spec = spec_from_file_location("linch_load_test", root / "scripts" / "load_test.py")
    assert spec and spec.loader
    harness = module_from_spec(spec)
    sys.modules[spec.name] = harness
    spec.loader.exec_module(harness)

    delay = 0.03

    class FragmentedReader:
        def __init__(self, chunks: list[bytes], eof_delay: float) -> None:
            self.chunks = iter(chunks)
            self.eof_delay = eof_delay
            self.done = False

        async def read(self, _limit: int) -> bytes:
            if not self.done:
                try:
                    chunk = next(self.chunks)
                except StopIteration:
                    self.done = True
                else:
                    await asyncio.sleep(0.001)
                    return chunk
            # Keep EOF behind the delayed queue so it cannot discard queued data.
            await asyncio.sleep(self.eof_delay)
            return b""

    class RecordingWriter:
        def __init__(self) -> None:
            self.writes: list[tuple[float, bytes]] = []

        def write(self, data: bytes) -> None:
            self.writes.append((time.monotonic(), data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def exercise(payload: bytes, *, eof_delay: float) -> None:
        writer = RecordingWriter()
        started = time.monotonic()
        await harness._Proxy("unused.sock", delay, 0).relay(
            FragmentedReader([bytes((byte,)) for byte in payload], eof_delay), writer
        )
        assert b"".join(chunk for _, chunk in writer.writes) == payload
        assert writer.writes
        first = writer.writes[0][0] - started
        spread = writer.writes[-1][0] - writer.writes[0][0]
        assert first >= delay * 0.65
        assert spread < delay * 2.5

    request = b"fragmented-request-0123456789"
    response = b"fragmented-response-9876543210"
    await exercise(request, eof_delay=delay * 2)
    await exercise(response, eof_delay=delay * 2)
    # EOF can arrive immediately after a fragmented burst.  The sentinel must
    # remain behind already timestamped bytes, otherwise it overtakes the queue
    # and silently drops the final request/response chunks.
    await exercise(request, eof_delay=0)


def _load_harness_module():
    from importlib.util import module_from_spec, spec_from_file_location

    root = Path(__file__).resolve().parents[2]
    spec = spec_from_file_location(
        "linch_load_test_stream_tests", root / "scripts" / "load_test.py"
    )
    assert spec and spec.loader
    harness = module_from_spec(spec)
    sys.modules[spec.name] = harness
    spec.loader.exec_module(harness)
    return harness


@pytest.mark.asyncio
async def test_stream_reader_waits_for_result_event_and_stable_tail(monkeypatch):
    """A terminal status without a result event must not end a stream early."""

    harness = _load_harness_module()
    import linch.run_stores.postgres as postgres

    class FakeStore:
        calls = 0

        def __init__(self, _dsn, *, pool):
            self.pool = pool

        async def load_events(self, _run_id, *, after_seq):
            del after_seq
            type(self).calls += 1
            if type(self).calls == 1:
                event = SimpleNamespace(type="assistant")
                return [
                    SimpleNamespace(seq=1, appended_at="2026-01-01T00:00:00+00:00", event=event)
                ]
            if type(self).calls == 2:
                event = SimpleNamespace(type="result")
                return [
                    SimpleNamespace(seq=2, appended_at="2026-01-01T00:00:00+00:00", event=event)
                ]
            return []

    monkeypatch.setattr(postgres, "PostgresRunStore", FakeStore)
    result: dict[str, Any] = {
        "in_order": True,
        "events": 0,
        "append_read_lag_ms": [],
        "reader_failures": 0,
        "terminal_tail_polls": 4,
    }

    class Pool:
        async def fetchval(self, _query, _rid):
            return "completed"

    await harness._read_stream(Pool(), "run-1", 0, result)
    assert FakeStore.calls >= 4
    assert result["completions"] == 1
    assert result["events"] == 2
    assert result["active_readers"] == 0
    assert result["reader_failures"] == 0


@pytest.mark.asyncio
async def test_fan_in_reader_peak_counts_only_active_followers(monkeypatch):
    """Fan-in subscriptions report actual active followers, not idle tasks."""

    harness = _load_harness_module()
    result: dict[str, Any] = {"reader_errors": {}}

    async def fake_read(_pool, rid, _interval, state):
        state["subscriptions"] = state.get("subscriptions", 0) + 1
        state["active_readers"] = state.get("active_readers", 0) + 1
        state["peak_readers"] = max(state.get("peak_readers", 0), state["active_readers"])
        await asyncio.sleep(0.03)
        state["completions"] = state.get("completions", 0) + 1
        state.setdefault("completed_run_ids", set()).add(rid)
        state["active_readers"] -= 1

    monkeypatch.setattr(harness, "_read_stream", fake_read)
    fan_state: dict[str, Any] = {"condition": asyncio.Condition(), "latest": None, "stop": False}
    tasks = [
        asyncio.create_task(harness._reader_loop(None, None, 0, result, fan_state=fan_state))
        for _ in range(4)
    ]
    async with fan_state["condition"]:
        fan_state["latest"] = "run-1"
        fan_state["condition"].notify_all()
    try:
        async with asyncio.timeout(1):
            while result.get("peak_readers", 0) < 4:
                await asyncio.sleep(0.001)
    finally:
        async with fan_state["condition"]:
            fan_state["stop"] = True
            fan_state["condition"].notify_all()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert result["peak_readers"] == 4
    assert result["subscriptions"] == 4
    assert result["active_readers"] == 0


def _acceptance_report(*, peak_readers: int = 100, chaos: dict[str, Any] | None = None):
    return {
        "workers": 2,
        "concurrency": 8,
        "target_rate": 1.0,
        "rtt_ms": 1,
        "enqueued": 1,
        "completed": 1,
        "failed": 0,
        "aborted": 0,
        "non_terminal": 0,
        "terminal_exactly_once": True,
        "duplicate_terminal": 0,
        "duplicate_save_notes": 0,
        "queue_remaining": 0,
        "event_sequence_contiguous": True,
        "event_audit": {},
        "stream": {
            "in_order": True,
            "terminal": True,
            "events": 1,
            "peak_concurrent_readers": peak_readers,
        },
        "chaos": chaos or {},
        "terminal_latency_ms": {"p99": 1.0},
        "runs_per_s": 1.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("experiment", ["quick", "point", "network", "streams"])
async def test_run_points_requires_fan_in_peak_for_every_experiment(monkeypatch, experiment):
    """A configured fan-in point cannot pass acceptance on idle subscribers."""

    harness = _load_harness_module()

    async def fake_run_point(_point, _dsn, *, quick=False):
        return _acceptance_report(peak_readers=0)

    monkeypatch.setattr(harness, "run_point", fake_run_point)
    point = SimpleNamespace(stream_fan_in=True, max_stream_readers=100, chaos=None)
    report, accepted = await harness._run_points(
        [point], SimpleNamespace(), SimpleNamespace(output=None), experiment=experiment
    )
    assert report["points"]
    assert accepted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("experiment", ["quick", "point", "network", "streams"])
async def test_run_points_requires_kill_recovery_for_every_experiment(monkeypatch, experiment):
    """A kill point cannot pass acceptance without fencing/recovery evidence."""

    harness = _load_harness_module()

    async def fake_run_point(_point, _dsn, *, quick=False):
        return _acceptance_report(chaos={"active_lease": {"epoch": 1}})

    monkeypatch.setattr(harness, "run_point", fake_run_point)
    point = SimpleNamespace(stream_fan_in=False, max_stream_readers=0, chaos="kill")
    report, accepted = await harness._run_points(
        [point], SimpleNamespace(), SimpleNamespace(output=None), experiment=experiment
    )
    assert report["points"]
    assert accepted is False


@pytest.mark.asyncio
async def test_run_points_accepts_healthy_quick_completion(monkeypatch):
    harness = _load_harness_module()

    async def fake_run_point(_point, _dsn, *, quick=False):
        return _acceptance_report()

    monkeypatch.setattr(harness, "run_point", fake_run_point)
    point = SimpleNamespace(stream_fan_in=False, max_stream_readers=0, chaos=None)
    _, accepted = await harness._run_points(
        [point], SimpleNamespace(), SimpleNamespace(output=None), experiment="quick", quick=True
    )
    assert accepted is True
