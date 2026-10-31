"""Small direct-Postgres smoke for the fleet harness (no Toxiproxy required)."""

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

import pytest


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
