"""Feature 2: lease, takeover time and epoch fencing between two stdio servers on one database.

Usage: python scenario_lease.py takeover [ttl_ms] | fence [ttl_ms]
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any

import h

from linch_rust import ConnectionClosed, RunLeased, Server, tool

PROMPT = "Call the tool slow_lookup with topic 'otters', then reply with exactly: DONE"


def slow_tool(started: asyncio.Event, release: asyncio.Event | None, calls: list[str]) -> Any:
    @tool(scope="read")
    async def slow_lookup(topic: str) -> str:
        """Look up a topic in the archive (slow)."""
        calls.append(topic)
        started.set()
        if release is not None:
            await release.wait()
        return f"{topic}: found"

    return slow_lookup


async def takeover(ttl: int, rep: int, proxy: h.CountingProxy) -> dict[str, Any]:
    out: dict[str, Any] = {"ttl_ms": ttl, "rep": rep, "load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        config = h.cfg(dsn, proxy.url, "lease-takeover", ttl=ttl)
        started = asyncio.Event()
        async with Server() as a, Server() as b:
            agent_a = await a.open_agent(config, tools=[slow_tool(started, asyncio.Event(), [])])  # pyright: ignore[reportArgumentType]
            agent_b = await b.open_agent(config, tools=[slow_tool(asyncio.Event(), None, [])])  # pyright: ignore[reportArgumentType]
            run = await agent_a.run("s1", PROMPT)
            await asyncio.wait_for(started.wait(), 120)
            # 2.1: B is refused while A holds the lease (A has the tool running).
            refused = None
            try:
                await (await agent_b.resume(run.id)).result()
            except RunLeased as leased:
                refused = {"owner": leased.owner, "expires_in": leased.expires_in}
            out["refused_while_held"] = refused
            # Let the heartbeat run a few times, then kill A at a random phase.
            await asyncio.sleep(1.0 + 0.37 * rep)
            t_kill = time.time()
            os.kill(a.pid, signal.SIGKILL)
            polls = 0
            t_ok = None
            while time.time() - t_kill < ttl / 1000 * 3 + 10:
                polls += 1
                r = await agent_b.resume(run.id)
                try:
                    it = aiter(r)
                    await anext(it)
                    t_ok = time.time()
                    result = await r.result()
                    break
                except RunLeased:
                    await asyncio.sleep(0.05)
            out["takeover_s"] = round(t_ok - t_kill, 3) if t_ok else None
            out["polls"] = polls
            out["result"] = getattr(result, "subtype", None)  # pyright: ignore[reportPossiblyUnboundVariable]
            rows = h.summarize(await agent_b.events(run.id))
            out["history"] = h.check_history(rows)
            out["events"] = h.types(rows)
            out["proxy_calls"] = proxy.n()
            with __import__("contextlib").suppress(ConnectionClosed, Exception):
                await run.result()
    return out


async def fence(ttl: int, proxy: h.CountingProxy) -> dict[str, Any]:
    out: dict[str, Any] = {"ttl_ms": ttl, "load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        config = h.cfg(dsn, proxy.url, "lease-fence", ttl=ttl)
        release = asyncio.Event()
        started = asyncio.Event()
        old_calls: list[str] = []
        async with Server() as a, Server() as b:
            agent_a = await a.open_agent(config, tools=[slow_tool(started, release, old_calls)])  # pyright: ignore[reportArgumentType]
            agent_b = await b.open_agent(config, tools=[slow_tool(asyncio.Event(), None, [])])  # pyright: ignore[reportArgumentType]
            run = await agent_a.run("s1", PROMPT)
            await asyncio.wait_for(started.wait(), 120)
            epoch0 = h.q(dsn, "SELECT epoch, status, lease_holder FROM linch_runs")
            os.kill(a.pid, signal.SIGSTOP)
            t_stop = time.time()
            try:
                await asyncio.sleep(ttl / 1000 * 1.6 + 0.5)
                result = None
                for _ in range(80):
                    r = await agent_b.resume(run.id)
                    try:
                        result = await r.result()
                        break
                    except RunLeased:
                        await asyncio.sleep(0.1)
                out["b_takeover_s"] = round(time.time() - t_stop, 2)
                settled = h.summarize(await agent_b.events(run.id))
                epoch1 = h.q(dsn, "SELECT epoch, status, lease_holder FROM linch_runs")
            finally:
                os.kill(a.pid, signal.SIGCONT)
            out["b_result"] = getattr(result, "subtype", None)
            out["epoch_before_after_b"] = [epoch0, epoch1]
            release.set()
            a_end: Any = None
            try:
                a_end = await asyncio.wait_for(run.result(), 40)
                a_end = f"returned {getattr(a_end, 'subtype', a_end)!r}"
            except Exception as error:
                a_end = f"{type(error).__name__}: {error}"
            out["a_run_ended_with"] = a_end
            await asyncio.sleep(1.0)
            after = h.summarize(await agent_b.events(run.id))
            epoch2 = h.q(dsn, "SELECT epoch, status, lease_holder FROM linch_runs")
            out["epoch_after_a_woke"] = epoch2
            out["events_equal_after_a_woke"] = [(e["seq"], e["type"]) for e in settled] == [
                (e["seq"], e["type"]) for e in after
            ]
            out["history_after"] = h.check_history(after)
            out["events_settled"] = h.types(settled)
            out["events_after"] = h.types(after)
            out["a_tool_calls"] = len(old_calls)
            out["proxy_calls"] = proxy.n()
    return out


async def main() -> None:
    proxy = h.CountingProxy()
    mode = sys.argv[1]
    ttl = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if mode == "takeover":
        res = []
        for rep in range(reps):
            h.log("takeover", ttl, rep)
            r = await takeover(ttl, rep, proxy)
            h.log(
                {
                    k: r.get(k)
                    for k in ("takeover_s", "polls", "refused_while_held", "result", "proxy_calls")
                }
            )
            res.append(r)
        h.dump(f"lease_takeover_{ttl}.json", res)
    else:
        r = await fence(ttl, proxy)
        h.log({k: v for k, v in r.items() if k not in ("events_settled", "events_after")})
        h.dump(f"lease_fence_{ttl}.json", r)
    proxy.close()


asyncio.run(main())
