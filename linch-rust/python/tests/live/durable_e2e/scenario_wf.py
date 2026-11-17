"""Feature 4: workflows (#160 #162 #165) against a tenant `linch-server --config`, real vLLM, real
Postgres. Clients are subprocesses so they can be killed for real.

Usage: python scenario_wf.py [a b c d e f g h]...
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import h

from linch_rust import Server

HERE = Path(__file__).resolve().parent
TOKEN = "tok-du-wf-0001"
PY = os.environ.get("E2E_PYTHON", sys.executable)


class Ctx:
    def __init__(self, dsn: str, proxy: h.CountingProxy) -> None:
        self.dsn = dsn
        self.proxy = proxy
        allow = h.tenant(dsn, urls=[proxy.url], workflow={}, max_queued_jobs=8)
        self.sconfig: dict[str, Any] = {
            "socket": "",
            "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}},
        }
        self.dir = Path(__import__("tempfile").mkdtemp(prefix="du-wf-"))
        self.sconfig["socket"] = str(self.dir / "s.sock")
        self.srv = h.Srv(self.sconfig, directory=self.dir)
        self.config = h.cfg(
            dsn, proxy.url, "acme", ttl=3000, spec={"max_turns": 4, "max_output_tokens": 200}
        )
        self.logs: list[str] = []

    async def start(self) -> None:
        self.srv = h.Srv(self.sconfig, directory=self.dir)
        await self.srv.start()

    async def kill(self) -> None:
        await self.srv.kill()
        self.logs.append(self.srv.log)

    def spec(self, mode: str, run_id: str, **params: Any) -> dict[str, Any]:
        return {
            "address": self.srv.addresses["socket"],
            "token": TOKEN,
            "config": self.config,
            "run_id": run_id,
            "mode": mode,
            "params": params,
        }

    async def spawn(self, mode: str, run_id: str, **params: Any) -> Client:
        c = Client(self, self.spec(mode, run_id, **params))
        await c.start()
        return c

    async def client(self, mode: str, run_id: str, timeout: float = 300, **params: Any) -> dict:
        c = await self.spawn(mode, run_id, **params)
        return await c.finish(timeout)


class Client:
    def __init__(self, ctx: Ctx, spec: dict[str, Any]) -> None:
        self.ctx = ctx
        self.spec = spec
        self.lines: list[dict[str, Any]] = []
        self.calls0 = ctx.proxy.n()
        self.t0 = time.time()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            PY,
            str(HERE / "wf_client.py"),
            json.dumps(self.spec),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        while line := await self.proc.stdout.readline():
            try:
                rec = json.loads(line)
            except Exception:
                rec = {"raw": line.decode(errors="replace")}
            rec["t"] = round(time.time() - self.t0, 2)  # pyright: ignore[reportArgumentType]
            self.lines.append(rec)

    async def wait_note(self, note: str, timeout: float = 120) -> None:
        await h.until(
            lambda: any(r.get("note") == note for r in self.lines), timeout=timeout, what=note
        )

    async def finish(self, timeout: float = 300) -> dict[str, Any]:
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        await self._reader
        err = (
            (await self.proc.stderr.read()).decode(errors="replace")[-400:]
            if self.proc.stderr
            else ""
        )
        kinds = Counter(r["ev"]["kind"] for r in self.lines if "ev" in r)
        end = next((r for r in self.lines if "end" in r), None)
        return {
            "rc": self.proc.returncode,
            "end": end,
            "kinds": dict(kinds),
            "calls": self.ctx.proxy.n() - self.calls0,
            "seconds": round(time.time() - self.t0, 2),
            "stderr": err if self.proc.returncode not in (0, None) else "",
            "events": [r["ev"] for r in self.lines if "ev" in r],
        }


def brief(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("rc", "end", "kinds", "calls", "seconds", "stderr") if k in r}


async def scenario_a(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.1 kill the client between stages; replay with zero provider calls; edited prompt; rerun."""
    a: dict[str, Any] = {}
    ctx.proxy.reset()
    a["crash_after_stage1"] = brief(await ctx.client("p3", "wf-A", crash="stage1"))
    a["resume_after_stage1"] = brief(await ctx.client("p3", "wf-A"))
    stage3_calls = a["resume_after_stage1"]["calls"]
    a["stage3_calls_nominal"] = stage3_calls
    a["rerun_finished"] = brief(await ctx.client("p3", "wf-A"))
    # killed after the step
    a["crash_after_stage2"] = brief(await ctx.client("p3", "wf-A2", crash="stage2"))
    a["resume_after_stage2"] = brief(await ctx.client("p3", "wf-A2"))
    # 4.3 an edited prompt re-runs only its own call
    a["edited_prompt"] = brief(
        await ctx.client("p3", "wf-A", a_prompt="Please reply with exactly this word: ALPHA")
    )
    out["A_client_kill_and_edit"] = a


async def scenario_b(ctx: Ctx, out: dict[str, Any], stage3_calls: int) -> None:
    """4.2 kill the SERVER mid-way (during stage 3's first provider call); new server; resume."""
    b: dict[str, Any] = {}
    ctx.proxy.reset()
    killed: dict[str, Any] = {}

    def on_arrive(rec: dict[str, Any]) -> None:
        if ctx.proxy.n() == 3 and not killed:
            rec["drop"] = True
            ctx.proxy.hold = True
            killed["t"] = time.time()
            os.kill(ctx.srv.pid, 9)

    ctx.proxy.on_arrive.append(on_arrive)
    first = await ctx.client("p3", "wf-B", timeout=300)
    b["first_drive"] = brief(first)
    b["calls_until_kill"] = ctx.proxy.n()
    ctx.proxy.hold = False
    ctx.proxy.released.set()
    ctx.proxy.on_arrive.clear()
    await ctx.kill()
    await ctx.start()
    ctx.proxy.released.clear()
    t0 = time.time()
    second = await ctx.client("p3", "wf-B", timeout=300)
    b["second_drive_new_server"] = brief(second)
    b["second_drive_s"] = round(time.time() - t0, 2)
    b["calls_second_drive"] = second["calls"]
    b["stage3_calls_nominal"] = stage3_calls
    b["repeated_calls"] = (b["calls_until_kill"] - 2) + second["calls"] - stage3_calls
    out["B_server_kill"] = b


async def scenario_d(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.4 interrupt answered after a server restart; 4.5 changed draft is stale."""
    d: dict[str, Any] = {}
    ctx.proxy.reset()
    d["i1_first"] = brief(await ctx.client("interrupt", "wf-I1", draft="v1"))
    await ctx.kill()
    await ctx.start()
    d["i1_answer_after_restart"] = brief(
        await ctx.client("interrupt", "wf-I1", draft="v1", resume={"approve": True})
    )
    d["i1_again"] = brief(await ctx.client("interrupt", "wf-I1", draft="v1"))
    # 4.5
    d["i2_first"] = brief(await ctx.client("interrupt", "wf-I2", draft="v1"))
    d["i2_changed_draft_with_answer"] = brief(
        await ctx.client("interrupt", "wf-I2", draft="v2", resume={"approve": True})
    )
    d["i2_answer_for_v2"] = brief(
        await ctx.client("interrupt", "wf-I2", draft="v2", resume={"approve": True})
    )
    d["proxy_calls"] = ctx.proxy.n()
    out["D_interrupts"] = d


async def scenario_e(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.6 two clients on one run_id."""
    e: dict[str, Any] = {}
    c1 = await ctx.spawn("slow", "wf-E", nap=8)
    await c1.wait_note("napping")
    c2 = await ctx.client("slow", "wf-E", timeout=60, nap=8)
    e["second_client"] = brief(c2)
    e["first_client"] = brief(await c1.finish(60))
    e["third_after_finish"] = brief(await ctx.client("slow", "wf-E", nap=8))
    out["E_held"] = e


async def scenario_f(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.7 principal."""
    f: dict[str, Any] = {}
    ada = {"tenant": "acme", "user": "ada", "roles": ["ops", "dev"]}
    f["start_ada"] = brief(await ctx.client("slow", "wf-P", nap=0.1, principal=ada))
    f["resume_bob"] = brief(
        await ctx.client(
            "slow",
            "wf-P",
            nap=0.1,
            principal={"tenant": "acme", "user": "bob", "roles": ["ops", "dev"]},
        )
    )
    f["resume_none"] = brief(await ctx.client("slow", "wf-P", nap=0.1))
    f["resume_ada_roles_reordered"] = brief(
        await ctx.client(
            "slow",
            "wf-P",
            nap=0.1,
            principal={"tenant": "acme", "user": "ada", "roles": ["dev", "ops"]},
        )
    )
    f["resume_ada_fewer_roles"] = brief(
        await ctx.client(
            "slow", "wf-P", nap=0.1, principal={"tenant": "acme", "user": "ada", "roles": ["ops"]}
        )
    )
    f["start_without_then_with"] = [
        brief(await ctx.client("slow", "wf-Q", nap=0.1)),
        brief(await ctx.client("slow", "wf-Q", nap=0.1, principal=ada)),
    ]
    out["F_principal"] = f


async def scenario_g(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.8 run_or_resume on a stored, a running and a finished run."""
    g: dict[str, Any] = {}
    ctx.proxy.reset()
    g["not_stored"] = brief(
        await ctx.client("run_or_resume", "roa-1", prompt="Reply with exactly: PONG")
    )
    g["finished_again"] = brief(
        await ctx.client("run_or_resume", "roa-1", prompt="Reply with exactly: PONG")
    )
    # A run another server drives now: held in flight at the proxy.
    ctx.proxy.reset()
    ctx.proxy.hold = True
    async with Server.connect(ctx.srv.addresses["socket"], token=TOKEN) as server:
        agent = await server.open_agent(ctx.config)  # pyright: ignore[reportArgumentType]
        run = await agent.run("roa-3", "Reply with exactly: PONG", run_id="roa-3")
        await h.until(lambda: ctx.proxy.n() >= 1, timeout=30, what="the provider call")
        g["driven_by_other_wait_1s"] = brief(
            await ctx.client(
                "run_or_resume", "roa-3", timeout=60, prompt="Reply with exactly: PONG", wait=1.0
            )
        )
        # The holder dies (kill -9 of the server): the lease runs out, then it continues.
        ctx.proxy.on_arrive.append(lambda rec: rec.__setitem__("drop", True))
        await ctx.kill()
        ctx.proxy.hold = False
        ctx.proxy.released.set()
        del run
    await ctx.start()
    ctx.proxy.released.clear()
    ctx.proxy.on_arrive.clear()
    n0 = ctx.proxy.n()
    t0 = time.time()
    g["after_holder_killed"] = brief(
        await ctx.client(
            "run_or_resume", "roa-3", timeout=120, prompt="Reply with exactly: PONG", wait=30.0
        )
    )
    g["after_holder_killed"]["calls"] = ctx.proxy.n() - n0
    g["after_holder_killed_s"] = round(time.time() - t0, 2)
    out["G_run_or_resume"] = g


async def scenario_h(ctx: Ctx, out: dict[str, Any]) -> None:
    """4.9 max_cost_usd on an unpriced agent."""
    ctx.proxy.reset()
    out["H_unpriced_cost_cap"] = {
        "max_cost_usd": brief(
            await ctx.client("slow", "wf-H", nap=0.1, budget={"max_cost_usd": 1.0})
        ),
        "max_tokens_ok": brief(
            await ctx.client("slow", "wf-H2", nap=0.1, budget={"max_tokens": 1000})
        ),
        "calls": ctx.proxy.n(),
    }


async def main() -> None:
    which = sys.argv[1:] or list("abcdefgh")
    proxy = h.CountingProxy()
    out: dict[str, Any] = {"load": h.load(), "binary": os.environ.get("LINCH_SERVER_BIN")}
    with h.schema() as dsn:
        ctx = Ctx(dsn, proxy)
        await ctx.start()
        try:
            stage3 = 0
            if "a" in which:
                await scenario_a(ctx, out)
                stage3 = out["A_client_kill_and_edit"]["stage3_calls_nominal"]
                h.dump("wf_a.json", out["A_client_kill_and_edit"])
                h.log("A", json.dumps(out["A_client_kill_and_edit"], default=str)[:3000])
            if "b" in which:
                await scenario_b(ctx, out, stage3 or 1)
                h.dump("wf_b.json", out["B_server_kill"])
                h.log("B", json.dumps(out["B_server_kill"], default=str)[:3000])
            for key, fn, name in (
                ("d", scenario_d, "D_interrupts"),
                ("e", scenario_e, "E_held"),
                ("f", scenario_f, "F_principal"),
                ("g", scenario_g, "G_run_or_resume"),
                ("h", scenario_h, "H_unpriced_cost_cap"),
            ):
                if key in which:
                    await fn(ctx, out)
                    h.dump(f"wf_{key}.json", out[name])
                    h.log(key.upper(), json.dumps(out[name], default=str)[:3500])
        finally:
            await ctx.kill()
            out["server_stderr"] = "".join(ctx.logs)[-1500:]
            h.dump("wf_all.json", out)
    proxy.close()


asyncio.run(main())
