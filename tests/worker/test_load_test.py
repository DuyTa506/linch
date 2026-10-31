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
    workers = [point["clients"][f"w-{i}"] for i in range(2)]
    assert sum(w["executed_jobs"] for w in workers) > 0
    for metrics in workers:
        assert metrics["dequeue_calls"] >= metrics["empty_dequeues"] > 0
        assert metrics["reset_queries"] > 0
        assert metrics["notifications_by_operation"]["insert"] > 0
        assert all(count >= 0 for count in metrics["notifications_by_operation"].values())


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


@pytest.mark.skipif(not os.getenv("AGENT_KIT_TEST_PG_DSN"), reason="Postgres DSN required")
async def test_recovery_deadline_covers_every_orphan_not_only_the_selected_run():
    import importlib.util

    import asyncpg

    script = Path(__file__).resolve().parents[2] / "scripts/load_test.py"
    spec = importlib.util.spec_from_file_location("load_orphans", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = await asyncpg.connect(os.environ["AGENT_KIT_TEST_PG_DSN"])
    try:
        await conn.execute(
            "CREATE TEMP TABLE load_claims(run_id text,owner text,epoch int,at timestamptz)"
        )
        await conn.executemany(
            "INSERT INTO load_claims VALUES($1,$2,$3,to_timestamp($4::float))",
            [
                ("a", "w-0", 1, 999.0),
                ("a", "w-1", 2, 1005.0),
                ("b", "w-0", 1, 999.0),
                ("b", "w-2", 2, 1006.9),
                ("other", "w-1", 1, 999.0),
                ("other", "w-2", 2, 1010.0),
                ("later", "w-0", 1, 1001.0),
                ("later", "w-1", 2, 1011.0),
            ],
        )
        chaos = {"lease_owner": "w-0", "injected_at": 1000.0}
        recovery = await module.recovery_after(conn, chaos)
        assert recovery["runs"] == 2 and recovery["run_id"] == "b"
        assert recovery["seconds"] == pytest.approx(6.9)
        assert module.recovered({"recovery": recovery})
        await conn.execute("INSERT INTO load_claims VALUES('b','w-3',3,to_timestamp(1012))")
        assert (await module.recovery_after(conn, chaos))["seconds"] == pytest.approx(6.9)
        await conn.execute("""INSERT INTO load_claims VALUES
            ('c','w-0',1,to_timestamp(999)),('c','w-2',2,to_timestamp(1007.1))""")
        assert not module.recovered({"recovery": await module.recovery_after(conn, chaos)})
    finally:
        await conn.close()


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
