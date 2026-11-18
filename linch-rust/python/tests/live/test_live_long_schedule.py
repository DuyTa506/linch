"""Live e2e (area long): a 60 s heartbeat and a cron schedule on a real worker.

Expectations L5-L7 and M2 of `expectations/long.md`. The tenant keeps
the documented 60 s `min_schedule_interval_ms` (the helper's 1000 is removed).
The provider is reached through the body-logging tee, so each fire's first
request can be compared with the previous fire's reply. Raw data goes to
`$E2E_RESULTS/schedule_*.json`.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import pytest
from e2e_long_support import MODEL, Tee, dump, load, msg_text
from live_core_support import needs_vllm
from live_sched_support import (
    TOKEN,
    live_config,
    pg_dsn,
    query,
    seqs_increasing,
    serving,
    tenant_allow,
    until,
    vllm,
)

from linch_rust import InitEvent, NotAllowed, ResultEvent, ScheduleFireEvent

__all__ = ["pg_dsn", "vllm"]

pytestmark = [pytest.mark.live, needs_vllm]

PROMPT = (
    "Check-in. Reply with CHECK-IN followed by the number of earlier check-in "
    "messages you can see in this conversation (0 if none). Nothing else."
)


def parse(ts: Any) -> float:
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


async def test_a_60s_heartbeat_and_a_cron_schedule_fire_on_time(vllm: str, pg_dsn: str) -> None:
    tee = Tee("schedule")
    allow = tenant_allow(pg_dsn, provider_base_urls=[tee.url])
    del allow["min_schedule_interval_ms"]  # the documented default (60 s) applies
    tenants = {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}}
    out: dict[str, Any] = {"load_at_start": load(), "poll_interval_ms": 1000}
    config = live_config(vllm, "acme", pg_dsn, spec={"max_output_tokens": 60})
    config["provider"] = {"id": "vllm", "base_url": tee.url}  # type: ignore[index]
    try:
        async with (
            serving(pg_dsn, tenants=tenants, worker={"poll_interval_ms": 1000}) as live,
            live.connect() as server,
        ):
            agent = await server.open_agent(config)

            # L6: faster than the minimum is refused.
            with pytest.raises(NotAllowed) as refused:
                await agent.create_schedule(PROMPT, interval_ms=30_000, id="too-fast")
            out["L6_refused"] = {"code": refused.value.code, "data": refused.value.data}

            created = time.time()
            beat = await agent.heartbeat("long-beat", interval_ms=60_000, prompt=PROMPT, id="hb")
            cron = await agent.create_schedule(PROMPT, cron="* * * * *", id="cr")
            out["created_at_client"] = created
            out["hb_created"] = beat
            out["cron_created"] = cron

            async def enough() -> bool:
                done = {"hb": 0, "cr": 0}
                for job in await agent.jobs():
                    if job["state"] == "done" and job.get("schedule_id") in done:
                        done[job["schedule_id"]] += 1  # pyright: ignore[reportTypedDictNotRequiredAccess]
                return done["hb"] >= 3 and done["cr"] >= 2

            await until(enough, timeout=330, every=2, what="3 heartbeat fires and 2 cron fires")
            out["load_at_end"] = load()
            jobs = await agent.jobs(limit=100)
            out["jobs"] = jobs
            fires: dict[str, list[dict[str, Any]]] = {"hb": [], "cr": []}
            for job in sorted(jobs, key=lambda j: j.get("fire_at") or ""):
                sid = job.get("schedule_id")
                if sid not in fires or job["state"] != "done":
                    continue
                events = await agent.events(job["run_id"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
                assert seqs_increasing(events)
                init = next(e for e in events if isinstance(e.event, InitEvent))
                fire = next(e.event for e in events if isinstance(e.event, ScheduleFireEvent))
                last = events[-1].event
                assert isinstance(last, ResultEvent)
                fires[sid].append(
                    {
                        "run_id": job["run_id"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                        "session_id": job.get("session_id"),
                        "fire_at_ms": fire.fire_at_ms,
                        "init_ts_ms": init.ts_ms,
                        "job_created_at": job.get("created_at"),
                        "job_fire_at": job.get("fire_at"),
                        "start_lag_s": (init.ts_ms - fire.fire_at_ms) / 1000,
                        "queue_lag_s": parse(job.get("created_at")) - fire.fire_at_ms / 1000,
                        "final_text": last.final_text,
                        "subtype": last.subtype,
                    }
                )
            out["fires"] = fires
            await agent.delete_schedule("hb")
            await agent.delete_schedule("cr")
    finally:
        tee.close()
        out["tee_requests"] = [
            {
                "ts": r["ts"],
                "messages": len(r["body"]["messages"]),
                "prompt_tokens": r.get("prompt_tokens"),
                "cached_tokens": r.get("cached_tokens"),
                "status": r.get("status"),
                "last_user": msg_text(r["body"]["messages"][-1])[:120],
                "reply": r.get("reply"),
            }
            for r in tee.chat()
        ]
        dump("schedule_heartbeat_cron", out)

    hb = fires["hb"][:3]
    assert len(hb) == 3
    assert {f["session_id"] for f in hb} == {"long-beat"}, "every fire continues one session"
    assert len({f["run_id"] for f in hb}) == 3
    assert all(f["subtype"] == "success" for f in hb)
    # L5: fire n+1's first request holds fire n's prompt and reply.
    by_run = [r for r in tee.chat() if msg_text(r["body"]["messages"][-1]).startswith("Check-in")]
    counts = [len(r["body"]["messages"]) for r in by_run]
    assert {2, 4, 6} <= set(counts), (
        counts
    )  # cron fires start fresh sessions (2); the heartbeat grows
    assert any(
        "CHECK-IN" in msg_text(m)
        for m in max(by_run, key=lambda r: len(r["body"]["messages"]))["body"]["messages"][:-1]
    ), "the last fire sees an earlier reply"
    # M2: jitter of the interval fires (relative to the due time on the server clock).
    for f in hb:
        assert abs(f["queue_lag_s"]) < 5, f
    # L7: a cron fire is due at second 0 of a minute.
    for f in fires["cr"][:2]:
        assert (f["fire_at_ms"] // 1000) % 60 == 0, f
        assert abs(f["queue_lag_s"]) < 5, f
    rows = query(pg_dsn, "SELECT count(*) FROM linch_worker_history WHERE schedule_id = 'hb'")
    assert rows[0][0] >= 3
    assert MODEL
