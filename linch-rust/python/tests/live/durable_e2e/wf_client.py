"""A workflow client process (a subprocess of scenario_wf.py so it can be killed for real).

argv[1] is a JSON object: address, token, config, run_id, mode, params. Prints one JSON line per
workflow event and a last line `{"end": ...}`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
import time
from typing import Any

from linch_rust import (
    Budget,
    LinchError,
    Server,
    WorkflowContext,
    WorkflowSuspended,
    run_or_resume,
    run_workflow,
)

SCHEMA = {
    "name": "tally",
    "schema": {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "words": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["count", "words"],
    },
}


def say(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, default=str), flush=True)


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def p3(wf: WorkflowContext, p: dict[str, Any]) -> Any:
    a_prompt = p.get("a_prompt", "Reply with exactly: ALPHA")
    await wf.phase("stage1")
    words = await wf.parallel(
        [
            lambda: wf.agent(a_prompt, label="a"),
            lambda: wf.agent("Reply with exactly: BETA", label="b"),
        ]
    )
    if p.get("crash") == "stage1":
        die()
    await wf.phase("stage2")
    joined = await wf.step("combine", lambda: " ".join(w.strip().strip(".").upper() for w in words))
    if p.get("crash") == "stage2":
        die()
    await wf.phase("stage3")
    rep = await wf.agent_result(
        f"The words are: {joined}. Return the count of words and the list of words.",
        output_schema=SCHEMA,  # type: ignore[arg-type]
    )
    return {"joined": joined, "structured": rep.structured}


async def interrupt_wf(wf: WorkflowContext, p: dict[str, Any]) -> Any:
    draft = p.get("draft", "first")
    ok = await wf.interrupt("approve", {"draft": draft})
    if not ok:
        return "withdrawn"
    return await wf.step("sign", lambda: f"signed {draft}", key=f"c-{draft}")


async def slow_wf(wf: WorkflowContext, p: dict[str, Any]) -> Any:
    async def nap() -> str:
        say({"note": "napping"})
        await asyncio.sleep(p.get("nap", 6))
        return "rested"

    return await wf.step("nap", nap)


async def main() -> None:
    spec = json.loads(sys.argv[1])
    p = spec.get("params", {})
    mode = spec["mode"]
    t0 = time.time()
    try:
        async with Server.connect(spec["address"], token=spec["token"]) as server:
            agent = await server.open_agent(spec["config"])
            say({"note": "opened", "t": time.time() - t0})
            if mode == "run_or_resume":
                r = await run_or_resume(
                    agent, spec["run_id"], p["prompt"], wait=p.get("wait", 60.0)
                )
                say(
                    {
                        "end": "ok",
                        "type": type(r).__name__,
                        "subtype": getattr(r, "subtype", None),
                        "final": getattr(r, "final_text", None),
                    }
                )
                return
            fn = {"p3": p3, "interrupt": interrupt_wf, "slow": slow_wf}[mode]

            def on_event(e: Any) -> None:
                say({"ev": dataclasses.asdict(e)})

            budget = Budget(**p["budget"]) if p.get("budget") else None
            value = await run_workflow(
                agent,
                lambda wf: fn(wf, p),
                run_id=spec["run_id"],
                resume=p.get("resume"),
                principal=p.get("principal"),
                budget=budget,
                on_event=on_event,
            )
            say({"end": "ok", "value": value})
    except WorkflowSuspended as s:
        say({"end": "suspended", "key": s.key, "payload": s.payload})
    except LinchError as error:
        say(
            {
                "end": "error",
                "cls": type(error).__name__,
                "msg": str(error)[:300],
                "kind": getattr(error, "kind", None),
                "code": getattr(error, "code", None),
            }
        )
    except BaseException as error:  # noqa: BLE001
        say({"end": "crash", "cls": type(error).__name__, "msg": str(error)[:300]})


asyncio.run(main())
