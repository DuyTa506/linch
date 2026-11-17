"""Feature 1: kill -9 of a stdio linch-server at several points, then resume on a new server.

Usage: python scenario_crash.py <point>...   (before_first, during_call, after_tool_end,
before_terminal, in_after_hook)
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any

import h

from linch_rust import ConnectionClosed, RunLeased, Server, callback, tool

COUNT = "Write the integers from 1 to 60, one per line, and nothing else."
PROMPT = "Call the tool lookup with topic 'otters', then reply with exactly: DONE"
TTL = int(os.environ.get("TTL", "3000"))


def make_tools(calls: list[str]) -> Any:
    @tool(scope="read")
    async def lookup(topic: str) -> str:
        """Look up a topic in the archive."""
        calls.append(topic)
        return f"{topic}: found"

    return lookup


async def one(point: str, proxy: h.CountingProxy) -> dict[str, Any]:
    out: dict[str, Any] = {"point": point, "load": h.load(), "ttl_ms": TTL}
    with h.schema() as dsn:
        proxy.reset()
        spec = {"max_output_tokens": 200} if point == "mid_stream" else None
        config = h.cfg(dsn, proxy.url, f"crash-{point}", ttl=TTL, spec=spec)
        holder: dict[str, Any] = {"pid": None, "armed": True, "bpc": 0, "stop": 0, "apc": 0}

        def kill(why: str) -> bool:
            if holder["armed"] and holder["pid"]:
                holder["armed"] = False
                holder["killed_at"] = time.time()
                holder["why"] = why
                holder["proxy_calls_at_kill"] = proxy.n()
                os.kill(holder["pid"], signal.SIGKILL)
                return True
            return False

        def callbacks() -> list[Any]:
            if point == "in_after_hook":

                @callback("after_provider_call", "apc", 1)
                async def apc(req: dict[str, Any]) -> dict[str, Any]:
                    holder["apc"] += 1
                    if holder["apc"] == 1 and kill("after_provider_call turn 1"):
                        await asyncio.sleep(30)
                    return {"action": "continue"}

                return [apc]

            @callback("before_provider_call", "bpc", 1)
            async def bpc(req: dict[str, Any]) -> dict[str, Any]:
                holder["bpc"] += 1
                if point == "before_first" and holder["bpc"] == 1:
                    if kill("before_provider_call turn 1"):
                        await asyncio.sleep(30)
                if point == "after_tool_end" and holder["bpc"] == 2:
                    if kill("before_provider_call turn 2"):
                        await asyncio.sleep(30)
                return {"action": "continue"}

            @callback("stop", "stp", 1)
            async def stp(req: dict[str, Any]) -> dict[str, Any]:
                holder["stop"] += 1
                if point == "before_terminal" and holder["stop"] == 1:
                    if kill("stop hook of the final turn"):
                        await asyncio.sleep(30)
                return {"action": "continue"}

            return [bpc, stp]

        if point == "during_call":
            proxy.hold = True

            def on_arrive(rec: dict[str, Any]) -> None:
                if proxy.n() == 1:
                    rec["drop"] = True
                    kill("provider call 1 in flight (held at the proxy)")

            proxy.on_arrive.append(on_arrive)

        first_tool_calls: list[str] = []
        async with Server() as server:
            holder["pid"] = server.pid
            agent = await server.open_agent(
                config, tools=[make_tools(first_tool_calls)], callbacks=callbacks()
            )
            run = await agent.run("s1", COUNT if point == "mid_stream" else PROMPT)
            try:
                if point == "mid_stream":
                    streamed = 0
                    async for env in run:
                        if env.event.type == "partial_assistant":
                            streamed += 1
                            if streamed == 3:
                                kill("3 partial_assistant events streamed")
                else:
                    await asyncio.wait_for(run.result(), 120)
                out["first_ended_without_kill"] = True
            except ConnectionClosed:
                pass
        proxy.hold = False
        proxy.released.set()
        out["kill"] = {
            k: holder.get(k) for k in ("why", "proxy_calls_at_kill", "bpc", "stop", "apc")
        }
        out["calls_before_resume"] = proxy.n()
        out["first_tool_calls"] = len(first_tool_calls)
        # What was durable at the kill.
        async with Server() as probe:
            pa = await probe.open_agent(config, tools=[make_tools([])], callbacks=callbacks())
            pre = h.summarize(await pa.events(run.id))
        out["durable_at_kill"] = h.types(pre)
        holder["armed"] = False
        holder["bpc"] = holder["stop"] = holder["apc"] = 0

        second_tool_calls: list[str] = []
        t0 = time.time()
        async with Server() as server2:
            agent2 = await server2.open_agent(
                config, tools=[make_tools(second_tool_calls)], callbacks=callbacks()
            )
            result = None
            leased_for = 0.0
            for _ in range(60):
                run2 = await agent2.resume(run.id)
                try:
                    result = await run2.result()
                    break
                except RunLeased as leased:
                    leased_for = time.time() - t0
                    await asyncio.sleep(max(leased.expires_in or 0.3, 0.2))
            events = await agent2.events(run.id)
        rows = h.summarize(events)
        out["resume"] = {
            "lease_wait_s": round(leased_for, 2),
            "total_s": round(time.time() - t0, 2),
            "result": getattr(result, "subtype", repr(result)),
            "tool_calls_after_resume": len(second_tool_calls),
        }
        out["proxy_calls_total"] = proxy.n()
        out["proxy_calls"] = [
            {k: c.get(k) for k in ("n", "state", "prompt_tokens", "completion_tokens")}
            for c in proxy.calls
        ]
        out["history"] = h.check_history(rows)
        out["events"] = rows
        pt, ct = proxy.tokens()
        out["proxy_tokens_completed_calls"] = {"prompt": pt, "completion": ct}
        out["turns"] = out["history"]["assistants"]
        out["extra_calls"] = out["proxy_calls_total"] - out["turns"]
    return out


async def main() -> None:
    proxy = h.CountingProxy()
    points = sys.argv[1:] or ["before_first", "during_call", "after_tool_end", "before_terminal"]
    for point in points:
        tag = os.environ.get("TAG", "")
        h.log("point", point)
        try:
            out = await one(point, proxy)
        except Exception as error:
            import traceback

            out = {"point": point, "error": repr(error), "tb": traceback.format_exc()}
        h.dump(f"crash_{point}{tag}.json", out)
        keys = ("kill", "durable_at_kill", "resume", "turns", "extra_calls", "proxy_calls_total")
        h.log(point, {k: out.get(k) for k in (*keys, "error")})
    proxy.close()


asyncio.run(main())
