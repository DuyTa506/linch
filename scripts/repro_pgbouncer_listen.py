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


async def observed(dsn: str, channel: str, admin: asyncpg.Connection) -> tuple[int, str | None]:
    events: list[str] = []
    pool = first = None
    error = None

    def listener(*args: object) -> None:
        events.append(str(args[-1]))

    try:
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=2, statement_cache_size=0)
        first = await pool.acquire()
        await first.add_listener(channel, listener)
        async with pool.acquire() as second:
            await second.fetchval("SELECT 1")
        await admin.execute("SELECT pg_notify($1, $2)", channel, "probe")
        await asyncio.sleep(0.5)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if first and pool:
            with contextlib.suppress(Exception):
                await first.remove_listener(channel, listener)
            with contextlib.suppress(Exception):
                await pool.release(first)
        if pool:
            with contextlib.suppress(Exception):
                await pool.close()
    return len(events), error


async def run() -> int:
    dsn = "postgresql://postgres@127.0.0.1:55432/postgres"
    admin = await asyncpg.connect(dsn, command_timeout=10)
    role = f"listen_probe_{uuid4().hex[:12]}"
    bouncer = None
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN')
        with tempfile.TemporaryDirectory(prefix="linch-listen-") as scratch:
            root, port = Path(scratch), free_port()
            (root / "users.txt").write_text(f'"{role}" ""\n')
            config = pgbouncer_config(
                port,
                *(str(root / name) for name in ("pgbouncer.log", "pgbouncer.pid", "users.txt")),
            ).replace("default_pool_size = 64", "default_pool_size = 1")
            config_path = root / "pgbouncer.ini"
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
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            pooled, pooled_error = await observed(
                f"postgresql://{role}@127.0.0.1:{port}/postgres", f"probe_{uuid4().hex}", admin
            )
            direct, direct_error = await observed(
                f"postgresql://{role}@127.0.0.1:55432/postgres", f"probe_{uuid4().hex}", admin
            )
            gap = pooled == 0 and direct == 1 and not pooled_error and not direct_error
            result = {
                "pooled_received": pooled,
                "direct_received": direct,
                "pooled_error": pooled_error,
                "direct_error": direct_error,
                "backend_pool_size": 1,
                "statement_cache_size": 0,
                "observed_gap": gap,
            }
            print(json.dumps(result, indent=2))
            return int(not result["observed_gap"])
    finally:
        if bouncer:
            await stop_process(bouncer, 5)
        with contextlib.suppress(Exception):
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
