#!/usr/bin/env python3
"""Target a worker at its durable started checkpoint and capture failure metadata."""

import json
import subprocess
import sys
from pathlib import Path


def controller() -> int:
    import asyncpg
    import load_test

    fetchrow = asyncpg.Pool.fetchrow
    summarize = load_test.summarize

    async def cold_owner(pool, query, *args, **kwargs):
        if query.startswith("SELECT id,lease_owner,lease_epoch FROM runs"):
            query = query.replace(
                " LIMIT 1", " AND checkpoint::jsonb->>'phase'='started' LIMIT 1", 1
            )
        return await fetchrow(pool, query, *args, **kwargs)

    async def diagnostics(pool, args):
        result = await summarize(pool, args)
        rows = await pool.fetch("""SELECT id,status,meta,checkpoint IS NULL checkpoint_is_null
            FROM runs WHERE status='failed'""")
        result["failed_details"] = [dict(row, meta=json.loads(row["meta"])) for row in rows]
        return result

    asyncpg.Pool.fetchrow = cold_owner
    load_test.summarize = diagnostics
    sys.argv = [str(Path(__file__).with_name("load_test.py")), *sys.argv[2:]]
    load_test.main()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        return controller()
    options = (
        "point --workers 4 --rate 10 --warmup 2 --duration 15 --drain-timeout 15 "
        "--rtt 1 --pool-size 16 --readers 0 --chaos kill "
        "--output /tmp/wp18-init-repro.json"
    ).split()
    command = [
        "flock",
        "--timeout",
        "900",
        "/tmp/wp8-cpu.lock",
        "timeout",
        "--signal=INT",
        "--kill-after=15s",
        "75s",
        sys.executable,
        str(Path(__file__)),
        "--inside",
        *options,
        *sys.argv[1:],
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
