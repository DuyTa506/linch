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


def test_pgbouncer_config_uses_transaction_pool_and_role_auth():
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    spec = importlib.util.spec_from_file_location("load_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.pgbouncer_config(56432, "/tmp/pg.log", "/tmp/pg.pid", "/tmp/users")
    assert "pool_mode = transaction" in config
    assert "postgres = host=127.0.0.1 port=55432 dbname=postgres" in config
    assert "connect_query" not in config
    assert "statement_cache" not in config


def test_pgbouncer_pool_disables_statement_cache(monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    spec = importlib.util.spec_from_file_location("load_test_pool", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = {}

    async def create_pool(dsn, **kwargs):
        captured.update(dsn=dsn, kwargs=kwargs)
        return object()

    monkeypatch.setattr(module.asyncpg, "create_pool", create_pool)
    args = SimpleNamespace(
        dsn="postgresql://postgres@127.0.0.1:55432/postgres",
        proxy_dsn="postgresql://load_user@127.0.0.1:56432/postgres",
        schema="load_test",
        pool_size=8,
        pgbouncer=True,
    )
    asyncio.run(module.pool_for(args, name="w-0"))
    assert captured["kwargs"]["statement_cache_size"] == 0
    assert captured["kwargs"]["server_settings"] == {}
    assert captured["dsn"].startswith("postgresql://w-0@")


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


@pytest.mark.parametrize("command,readers", [("network", 100), ("capacity", 1)])
async def test_experiment_commands_keep_witness_readers_and_reach_high_rates(
    monkeypatch, command, readers
):
    import importlib.util
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    spec = importlib.util.spec_from_file_location("load_grid", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = []

    async def point(args):
        seen.append(args)
        return {"latency_ms": {"p99": 1}, "invariant_errors": {}}

    monkeypatch.setattr(module, "run_point", point)
    await module.experiments(
        SimpleNamespace(command=command, readers=readers, rate=2, quick=False, output=None)
    )
    assert all(a.readers == readers for a in seen)
    if command == "capacity":
        assert max(a.rate for a in seen) >= 256


@pytest.mark.parametrize("seconds,expected", [(None, False), (6.9, True), (7, True), (7.1, False)])
def test_kill_recovery_gate_enforces_the_seven_second_deadline(seconds, expected):
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    spec = importlib.util.spec_from_file_location("load_recovery", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recovery = {"seconds": seconds} if seconds is not None else None
    assert module.recovered({"recovery": recovery}) is expected


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
