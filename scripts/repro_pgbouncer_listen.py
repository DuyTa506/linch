#!/usr/bin/env python3
"""Reproduce LISTEN loss through PgBouncer transaction pooling (pooled 0, direct 1)."""

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import asyncpg
from load_test import free_port, pgbouncer_config, stop_process


async def run() -> int:
    dsn = "postgresql://postgres@127.0.0.1:55432/postgres"
    admin = await asyncpg.connect(dsn, command_timeout=10)
    role = f"listen_probe_{uuid4().hex[:12]}"
    bouncer = pool = direct = None
    pooled_events: list[str] = []
    direct_events: list[str] = []
    pooled_error = None
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN')
        with tempfile.TemporaryDirectory(prefix="linch-listen-") as scratch:
            scratch_path, port = Path(scratch), free_port()
            authfile = scratch_path / "users.txt"
            authfile.write_text(f'"{role}" ""\n')
            config = pgbouncer_config(
                port,
                *(
                    str(scratch_path / name)
                    for name in ("pgbouncer.log", "pgbouncer.pid", "users.txt")
                ),
            )
            config = config.replace("default_pool_size = 64", "default_pool_size = 1")
            config_path = scratch_path / "pgbouncer.ini"
            config_path.write_text(config)
            bouncer = await asyncio.create_subprocess_exec(
                "/usr/sbin/pgbouncer",
                str(config_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(50):
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            app_dsn = f"postgresql://{role}@127.0.0.1:{port}/postgres"
            pool = await asyncpg.create_pool(
                app_dsn, min_size=2, max_size=2, statement_cache_size=0
            )
            channel = f"listen_probe_{uuid4().hex}"
            try:
                async with pool.acquire() as connection:
                    await connection.add_listener(
                        channel, lambda *args: pooled_events.append(args[-1])
                    )
                async with pool.acquire() as connection:
                    await connection.fetchval("SELECT 1")
            except Exception as exc:  # Record a rejected LISTEN as evidence too.
                pooled_error = f"{type(exc).__name__}: {exc}"
            await admin.execute("SELECT pg_notify($1, $2)", channel, "pooled")
            await asyncio.sleep(0.5)
            direct = await asyncpg.connect(dsn)
            direct_channel = f"listen_probe_{uuid4().hex}"
            await direct.add_listener(direct_channel, lambda *args: direct_events.append(args[-1]))
            await admin.execute("SELECT pg_notify($1, $2)", direct_channel, "direct")
            await asyncio.sleep(0.5)
            result = {
                "expected": {"pooled_received": 0, "direct_received": 1},
                "pooled_received": len(pooled_events),
                "direct_received": len(direct_events),
                "pooled_error": pooled_error,
                "backend_pool_size": 1,
                "statement_cache_size": 0,
                "observed_gap": not pooled_error and not pooled_events and len(direct_events) == 1,
            }
            print(json.dumps(result, indent=2))
            return int(not result["observed_gap"])
    finally:
        if pool:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pool.close(), 5)
        if direct:
            with contextlib.suppress(Exception):
                await direct.close()
        if bouncer:
            await stop_process(bouncer, 5)
        with contextlib.suppress(Exception):
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
