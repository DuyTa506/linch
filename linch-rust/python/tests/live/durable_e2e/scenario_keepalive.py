"""Does the server set TCP keepalive when the liveness times are below one second?"""

from __future__ import annotations

import asyncio
import sys

import h
from scenario_live import free_port, server, upgrade


async def probe(liveness: dict[str, int] | None) -> dict[str, object]:
    port = free_port()
    srv = await server(liveness, port)
    try:
        for _ in range(2):
            s, status = await asyncio.to_thread(upgrade, port)
            s.close()
            await asyncio.sleep(0.2)
        warnings = [ln for ln in srv.log.splitlines() if "keepalive" in ln]
        return {
            "liveness": liveness,
            "status": status,
            "keepalive_warnings": len(warnings),
            "sample": warnings[:1],
        }
    finally:
        await srv.kill()


async def main() -> None:
    rows = [
        await probe(None),
        await probe(
            {"keepalive_idle_ms": 1000, "keepalive_interval_ms": 1000, "keepalive_probes": 2}
        ),
        await probe(
            {"keepalive_idle_ms": 1000, "keepalive_interval_ms": 500, "keepalive_probes": 2}
        ),
        await probe(
            {"keepalive_idle_ms": 500, "keepalive_interval_ms": 1000, "keepalive_probes": 2}
        ),
        await probe(
            {"keepalive_idle_ms": 1500, "keepalive_interval_ms": 1500, "keepalive_probes": 2}
        ),
    ]
    for r in rows:
        h.log(r)
    h.dump("liveness_keepalive_units.json", rows)


asyncio.run(main())
sys.exit(0)
