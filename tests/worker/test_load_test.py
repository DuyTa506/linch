"""Small direct-Postgres smoke for the fleet harness (no Toxiproxy required)."""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


def test_fractional_rtt_is_rejected_before_starting_processes():
    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    result = subprocess.run(
        [sys.executable, str(script), "--rtt", "1.5", "--duration", "0"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "argument --rtt" in result.stderr


@pytest.mark.skipif(not os.getenv("AGENT_KIT_TEST_PG_DSN"), reason="Postgres DSN required")
async def test_two_process_workers_finish_once_without_duplicate_notes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "report.json"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(root / "scripts/load_test.py"),
        "point",
        "--direct",
        "--dsn",
        os.environ["AGENT_KIT_TEST_PG_DSN"],
        "--workers",
        "2",
        "--warmup",
        "0",
        "--duration",
        "3",
        "--rate",
        "4",
        "--ttft",
        "0.005",
        "--delta-window",
        "0.01",
        "--tool-low",
        "0.001",
        "--tool-high",
        "0.002",
        "--output",
        str(output),
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    assert proc.returncode == 0, (stderr + stdout).decode()[-4000:]
    point = json.loads(output.read_text())["points"][0]
    assert point["workers"] == 2
    assert point["runs"] > 0
    assert point["completed"] == point["terminal"] == point["runs"], point
    assert point["invariant_errors"] and not any(point["invariant_errors"].values()), point


def test_initialization_repro_selects_durable_started_victim(monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[2] / "scripts/repro_worker_initialization.py"
    spec = importlib.util.spec_from_file_location("initialization_repro", script)
    assert spec is not None and spec.loader is not None
    repro = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repro)
    captured = []
    victim = {"id": "initializing", "lease_owner": "worker-a", "lease_epoch": 1}

    class Pool:
        async def fetchrow(self, query, *args, **kwargs):
            captured.append((query, args, kwargs))
            return victim

    query = (
        "SELECT id,lease_owner,lease_epoch FROM runs "
        "WHERE status='running' AND lease_owner IS NOT NULL LIMIT 1"
    )

    async def summarize(pool, args):
        return {}

    def main():
        async def probe():
            pool = Pool()
            assert await pool.fetchrow(query, "arg", timeout=3) is victim
            await pool.fetchrow("SELECT unrelated", "other")

        asyncio.run(probe())

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(Pool=Pool))
    monkeypatch.setitem(sys.modules, "load_test", SimpleNamespace(main=main, summarize=summarize))
    monkeypatch.setattr(sys, "argv", [str(script), "--inside", "point", "--chaos", "kill"])
    assert repro.controller() == 0
    assert captured == [
        (
            query.replace(" LIMIT 1", " AND checkpoint::jsonb->>'phase'='started' LIMIT 1"),
            ("arg",),
            {"timeout": 3},
        ),
        ("SELECT unrelated", ("other",), {}),
    ]
