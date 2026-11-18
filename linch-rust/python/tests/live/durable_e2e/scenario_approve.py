"""Feature 3: approvals across restarts, cancel of a suspended run, the pruned approve of a
scheduled run (#165).

Usage: python scenario_approve.py approve cancel pruned
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import h

from linch_rust import (
    Allow,
    LinchError,
    Server,
    SuspendedEvent,
    tool,
)

PROMPT = "Call the tool save_note with text 'call back at noon', then reply with exactly: DONE"


def save_tool(calls: list[str]) -> Any:
    @tool(scope="write")
    async def save_note(text: str) -> str:
        """Save a note."""
        calls.append(text)
        return f"saved: {text}"

    return save_note


def perm() -> dict[str, Any]:
    return {"mode": "default", "project_root": "/"}


async def approve_after_kill(proxy: h.CountingProxy) -> dict[str, Any]:
    out: dict[str, Any] = {"load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        config = h.cfg(dsn, proxy.url, "appr", permissions=perm())
        c1: list[str] = []
        async with Server() as s1:
            agent = await s1.open_agent(config, tools=[save_tool(c1)])  # pyright: ignore[reportArgumentType]
            run = await agent.run("s1", PROMPT)
            suspended = await asyncio.wait_for(run.result(), 120)
            out["suspended"] = isinstance(suspended, SuspendedEvent)
            out["pending"] = list(getattr(suspended, "pending_request_ids", []))
            os.kill(s1.pid, signal.SIGKILL)
        out["tool_calls_before_approve"] = len(c1)
        out["proxy_calls_at_suspend"] = proxy.n()
        c2: list[str] = []
        async with Server() as s2:
            agent2 = await s2.open_agent(config, tools=[save_tool(c2)])  # pyright: ignore[reportArgumentType]
            t0 = time.time()
            # No lease wait expected: a suspended run refuses nobody.
            r = await agent2.approve(run.id, {i: Allow() for i in out["pending"]})
            result = await asyncio.wait_for(r.result(), 120)
            out["approve_s"] = round(time.time() - t0, 2)
            out["result"] = getattr(result, "subtype", repr(result))
            try:
                await (await agent2.approve(run.id, {i: Allow() for i in out["pending"]})).result()
                out["second_approve"] = "no error"
            except LinchError as error:
                out["second_approve"] = type(error).__name__
            rows = h.summarize(await agent2.events(run.id))
        out["tool_calls_after_approve"] = c2
        out["proxy_calls_total"] = proxy.n()
        out["history"] = h.check_history(rows)
        out["events"] = h.types(rows)
    return out


async def cancel_suspended(proxy: h.CountingProxy, after_restart: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"after_restart": after_restart, "load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        config = h.cfg(dsn, proxy.url, "canc", permissions=perm())
        s1 = Server()
        await s1.start()
        agent = await s1.open_agent(config, tools=[save_tool([])])  # pyright: ignore[reportArgumentType]
        run = await agent.run("s1", PROMPT)
        suspended = await asyncio.wait_for(run.result(), 120)
        assert isinstance(suspended, SuspendedEvent)
        pend = list(suspended.pending_request_ids)
        if after_restart:
            os.kill(s1.pid, signal.SIGKILL)
            await s1.close()
            s2 = Server()
            await s2.start()
            agent = await s2.open_agent(config, tools=[save_tool([])])  # pyright: ignore[reportArgumentType]
            server = s2
        else:
            server = s1
        t0 = time.time()
        cancelled = await agent.cancel(run.id)
        out["cancel_returned"] = cancelled
        out["cancel_s"] = round(time.time() - t0, 3)
        rows = h.summarize(await agent.events(run.id))
        out["events_tail"] = h.types(rows)[-4:]
        last = [r for r in rows if r["type"] == "result"]
        out["result_subtype"] = last[-1]["subtype"] if last else None
        try:
            await (await agent.approve(run.id, {i: Allow() for i in pend})).result()
            out["approve_after_cancel"] = "no error"
        except LinchError as error:
            out["approve_after_cancel"] = f"{type(error).__name__}"
        try:
            again = await agent.cancel(run.id)
            out["second_cancel"] = again
        except LinchError as error:
            out["second_cancel"] = f"{type(error).__name__}"
        # The session is free: a new run in it works.
        n0 = proxy.n()
        try:
            r2 = await agent.run("s1", "Reply with exactly: PONG")
            res2 = await asyncio.wait_for(r2.result(), 120)
            out["new_run_in_session"] = getattr(res2, "subtype", repr(res2))
        except LinchError as error:
            out["new_run_in_session"] = f"{type(error).__name__}: {error}"
        out["new_run_proxy_calls"] = proxy.n() - n0
        await server.close()
    return out


async def pruned(proxy: h.CountingProxy) -> dict[str, Any]:
    out: dict[str, Any] = {"load": h.load()}
    root = Path(tempfile.mkdtemp(prefix="du-ws-")).resolve()
    with h.schema() as dsn:
        proxy.reset()
        token = "tok-du-approve-0001"
        allow = h.tenant(
            dsn,
            urls=[proxy.url],
            workspace_roots=[str(root)],
            max_schedules=4,
            max_queued_jobs=8,
            min_schedule_interval_ms=1000,
        )
        config = h.cfg(
            dsn,
            proxy.url,
            "acme",
            spec={"max_output_tokens": 200},
            workspace=str(root),
            permissions={"mode": "default", "project_root": str(root)},
        )

        def server_config(retention: int) -> dict[str, Any]:
            return {
                "socket": "",
                "tenants": {"acme": {"tokens": [{"value": token}], "allow": allow}},
                "worker": {
                    "dsn": {"value": dsn},
                    "poll_interval_ms": 250,
                    "history_retention_ms": retention,
                },
            }

        def counts() -> dict[str, Any]:
            tables = h.q(
                dsn,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = "
                "current_schema() "
                "AND table_name LIKE 'linch_worker%%' ORDER BY 1",
            )
            res: dict[str, Any] = {}
            for (t,) in tables:
                res[t] = h.q(dsn, f'SELECT count(*) FROM "{t}"')[0][0]
            return res

        d1 = Path(tempfile.mkdtemp(prefix="du-s-"))
        cfg1 = server_config(1000)
        cfg1["socket"] = str(d1 / "s.sock")
        srv = h.Srv(cfg1, directory=d1)
        await srv.start()
        async with srv.connect(token) as server:
            agent = await server.open_agent(config)
            out["tools"] = None
            job = await agent.enqueue(
                "sched-1",
                "Call the tool Write with file_path 'note.txt' and content 'hi', then reply with "
                "exactly: DONE",
            )
            done = await agent.wait_job(job["job_id"], timeout=180)
            out["job_state"] = done["state"]
            run_id = done.get("run_id")
            events = await agent.events(run_id)
            last = events[-1].event
            out["run_state_after_job"] = type(last).__name__
            pend = list(getattr(last, "pending_request_ids", []))
            out["pending"] = pend
        out["counts_after_job"] = counts()
        await srv.kill()
        # Restart: the worker prunes on its first poll (retention 1 s, already past).
        await asyncio.sleep(1.5)
        d2 = Path(tempfile.mkdtemp(prefix="du-s-"))
        cfg2 = server_config(1)
        cfg2["socket"] = str(d2 / "s.sock")
        srv2 = h.Srv(cfg2, directory=d2)
        await srv2.start()
        await asyncio.sleep(3.0)
        out["counts_after_prune_while_suspended"] = counts()
        async with srv2.connect(token) as server:
            agent = await server.open_agent(config)
            r = await agent.approve(run_id, {i: Allow() for i in pend})
            result = await asyncio.wait_for(r.result(), 180)
            out["approve_result"] = getattr(result, "subtype", repr(result))
            out["file_written"] = (root / "note.txt").exists()
        await srv2.kill()
        await asyncio.sleep(1.0)
        d3 = Path(tempfile.mkdtemp(prefix="du-s-"))
        cfg3 = server_config(1)
        cfg3["socket"] = str(d3 / "s.sock")
        srv3 = h.Srv(cfg3, directory=d3)
        await srv3.start()
        await asyncio.sleep(3.0)
        out["counts_after_prune_when_terminal"] = counts()
        await srv3.kill()
        out["stderr_tail"] = (srv.log + srv2.log + srv3.log)[-600:]
        out["proxy_calls"] = proxy.n()
    return out


async def main() -> None:
    proxy = h.CountingProxy()
    which = sys.argv[1:] or ["approve", "cancel", "pruned"]
    if "approve" in which:
        r = await approve_after_kill(proxy)
        h.log("approve", {k: v for k, v in r.items() if k != "events"})
        h.dump("approve_after_kill.json", r)
    if "cancel" in which:
        res = [await cancel_suspended(proxy, False), await cancel_suspended(proxy, True)]
        for r in res:
            h.log("cancel", r)
        h.dump("cancel_suspended.json", res)
    if "pruned" in which:
        r = await pruned(proxy)
        h.log("pruned", r)
        h.dump("approve_pruned.json", r)
    proxy.close()


asyncio.run(main())
