"""Live toolsec: two tenants on one server, `allow.*` absent by default: every denied path, the limits,
isolation between tenants, the tool cache across principals, and no leak in a tenant's error text.

Expectations are in `toolsec/expectations.md` section 6.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from live_sched_support import pg_dsn
from ts_support import (
    FakeModel,
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    serving,
    tenant,
    text_of,
    workspace,
)

from linch_rust import (
    InitEvent,
    JobNotFound,
    LimitReached,
    NotAllowed,
    RunNotFound,
    tool,
)

__all__ = ["pg_dsn", "pytestmark"]

AREA = "tenants"
ACME, BETA = "tok-acme-ts-0001", "tok-beta-ts-0001"
SECRET_HEADER = "hdr-secret-6b1f9"


def messages_clean(messages: list[str], *forbidden: str) -> list[str]:
    return [
        f"{needle!r} in {message!r}"
        for message in messages
        for needle in forbidden
        if needle and needle in message
    ]


@needs_server
async def test_every_denied_by_default_path_returns_its_typed_error(pg_dsn: str) -> None:
    model = await plan_model([]).start()
    collected: list[str] = []
    try:
        with workspace() as root:
            allow = {
                "permission_modes": ["default"],
                "workspace_roots": [str(root)],
                "provider_base_urls": [model.url],
                "postgres_dsns": [pg_dsn],
            }
            tenants = {"acme": tenant(ACME, **allow), "beta": tenant(BETA, **allow)}
            async with serving(tenants) as proc:
                async with proc.connect(ACME) as server:

                    def base(**top: Any) -> Any:
                        return fake_config(model, namespace="acme", workspace=str(root), **top)

                    async def denied(kind: str, config: Any) -> None:
                        with pytest.raises(NotAllowed) as caught:
                            await server.open_agent(config)
                        collected.append(str(caught.value))
                        record(
                            AREA, f"6-denied-{kind}", kind=caught.value.kind, text=str(caught.value)
                        )
                        assert caught.value.kind == kind, (kind, str(caught.value))

                    await denied("provider", base(provider={"id": "scripted", "turns": []}))
                    cfg = base()
                    cfg["provider"] = {
                        **cfg["provider"],
                        "request": {"default_headers": {"x-a": "b"}},
                    }
                    await denied("provider_request", cfg)
                    await denied("execution", base(execution={"backend": "sandbox"}))
                    await denied("memory", base(memory={"recall": {"k": 5}, "max_memories": 10}))
                    await denied("task_ledger", base(task_ledger={"list": "ledger"}))
                    cfg = base()
                    cfg["provider"] = {"id": "vllm", "base_url": "http://127.0.0.1:1/v1"}
                    await denied("provider_base_url", cfg)
                    cfg = base()
                    del cfg["spec"]["max_turns"]
                    await denied("run_bound", cfg)
                    await denied("namespace", fake_config(model, namespace="beta"))
                    await denied(
                        "permission_mode",
                        base(permissions={"mode": "skip_dangerous", "project_root": str(root)}),
                    )
                    await denied(
                        "auto_allow",
                        base(
                            permissions={
                                "mode": "default",
                                "project_root": str(root),
                                "auto_allow": ["Read"],
                            }
                        ),
                    )
                    cfg = base()
                    cfg["spec"]["tool_cache"] = {"tools": ["x"]}
                    await denied("tool_cache", cfg)
                    await denied(
                        "workspace", fake_config(model, namespace="acme", workspace="/etc")
                    )
                    await denied(
                        "workspace",
                        fake_config(model, namespace="acme", workspace="/nonexistent-ts"),
                    )
                    await denied(
                        "schedule_tools",
                        base(schedule_tools=True, stores={"type": "postgres", "dsn": pg_dsn}),
                    )
                    await denied(
                        "mcp_command",
                        base(
                            mcp_servers=[
                                {
                                    "name": "m",
                                    "transport": {
                                        "type": "stdio",
                                        "command": "/bin/true",
                                        "args": [],
                                    },
                                }
                            ]
                        ),
                    )
                    await denied(
                        "postgres",
                        base(
                            stores={"type": "postgres", "dsn": "postgres://other@127.0.0.1:1/other"}
                        ),
                    )

                    # Bash is turned off silently, not refused
                    agent = await server.open_agent(base())
                    run = await agent.run("s1", "go")
                    init = [
                        e.event for e in await finish(agent, run) if isinstance(e.event, InitEvent)
                    ]
                    record(AREA, "6-bash-off", tools=init[0].tools if init else None)
                    assert init and "Bash" not in init[0].tools

                    # runtime paths on an agent that opened
                    with pytest.raises(NotAllowed) as impostor:
                        await (
                            await agent.run("s2", "go", principal={"tenant": "beta", "user": "eve"})
                        ).result()
                    collected.append(str(impostor.value))
                    assert impostor.value.kind == "principal"

                    from linch_rust import run_workflow

                    async def flow(wf: Any) -> str:
                        return "x"

                    with pytest.raises(NotAllowed) as no_workflow:
                        await run_workflow(agent, flow, run_id="wf-denied")
                    collected.append(str(no_workflow.value))
                    record(
                        AREA,
                        "6-denied-workflow",
                        kind=no_workflow.value.kind,
                        text=str(no_workflow.value),
                    )
                    assert no_workflow.value.kind == "workflow"

                    stored = await server.open_agent(
                        base(stores={"type": "postgres", "dsn": pg_dsn})
                    )
                    with pytest.raises(NotAllowed) as no_schedule:
                        await stored.create_schedule("go", interval_ms=120_000, id="x")
                    with pytest.raises(NotAllowed) as no_job:
                        await stored.enqueue("s9", "go")
                    collected += [str(no_schedule.value), str(no_job.value)]
                    record(
                        AREA,
                        "6-denied-schedule-job",
                        schedule=(no_schedule.value.kind, str(no_schedule.value)),
                        job=(no_job.value.kind, str(no_job.value)),
                    )
                    assert no_schedule.value.kind in {"schedules", "schedule_tools"}
                    assert no_job.value.kind == "background_runs"
                leaks = messages_clean(
                    collected, pg_dsn, "postgres:", str(proc.dir), ACME, BETA, SECRET_HEADER
                )
                record(AREA, "6-error-text-leaks", leaks=leaks, messages=len(collected))
                assert not leaks, leaks
    finally:
        await model.stop()


@needs_server
async def test_per_tenant_limits(pg_dsn: str) -> None:
    model = await plan_model([]).start()
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "workspace_roots": [str(root)],
                "provider_base_urls": [model.url],
                "postgres_dsns": [pg_dsn],
                "max_connections": 2,
                "max_agents": 2,
                "max_schedules": 1,
                "min_schedule_interval_ms": 60000,
                "max_queued_jobs": 4,
                "workflow": {"max_workflows": 1},
            }
            async with serving({"lim": tenant(ACME, **allow)}, http=True) as proc:
                one = proc.connect(ACME)
                two = proc.connect(ACME)
                await one.__aenter__()
                await two.__aenter__()
                with pytest.raises(LimitReached) as third:
                    await proc.connect(ACME).__aenter__()
                record(AREA, "6-limit-connections", kind=third.value.kind, max=third.value.max)
                assert third.value.kind == "connections"
                # HTTP upgrade with the tenant already at its cap
                status = await http_upgrade(proc.port, ACME)
                record(AREA, "6-limit-connections-http", status=status)
                assert status.startswith("HTTP/1.1 429")

                def config(name: str) -> Any:
                    return fake_config(
                        model,
                        namespace=f"lim/{name}",
                        stores={"type": "postgres", "dsn": pg_dsn},
                    )

                first = await one.open_agent(config("a"))
                await one.open_agent(config("b"))
                with pytest.raises(LimitReached) as agents:
                    await two.open_agent(config("c"))
                record(AREA, "6-limit-agents", kind=agents.value.kind, max=agents.value.max)
                assert agents.value.kind == "agents"

                with pytest.raises(NotAllowed) as fast:
                    await first.create_schedule("go", interval_ms=1000, id="fast")
                record(AREA, "6-schedule-interval", kind=fast.value.kind)
                assert fast.value.kind == "schedule_interval"
                await first.create_schedule("go", interval_ms=60_000, id="s-one")
                with pytest.raises(LimitReached) as extra:
                    await first.create_schedule("go", interval_ms=60_000, id="s-two")
                record(AREA, "6-limit-schedules", kind=extra.value.kind, max=extra.value.max)
                assert extra.value.kind == "schedules"

                from linch_rust import run_workflow

                release = asyncio.Event()
                entered = asyncio.Event()

                async def holding(wf: Any) -> str:
                    async def block() -> str:
                        entered.set()
                        await release.wait()
                        return "released"

                    return await wf.step("hold", block)

                async def quick(wf: Any) -> str:
                    return "x"

                held = asyncio.create_task(run_workflow(first, holding, run_id="wf-hold"))
                await asyncio.wait_for(entered.wait(), 20)
                with pytest.raises(LimitReached) as busy:
                    await run_workflow(first, quick, run_id="wf-second")
                record(AREA, "6-limit-workflows", kind=busy.value.kind, max=busy.value.max)
                release.set()
                assert await held == "released"
                assert busy.value.kind == "workflows"
                await two.__aexit__(None, None, None)
                await one.__aexit__(None, None, None)
    finally:
        await model.stop()


async def http_upgrade(port: int, token: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        "GET /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.read(512), 10)
    writer.close()
    return head.decode("latin-1").splitlines()[0]


@needs_server
async def test_tenants_cannot_see_each_others_runs_sessions_workflows_schedules_or_jobs(
    pg_dsn: str,
) -> None:
    model = await FakeModel(lambda seen: ("text", "pong")).start()
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "workspace_roots": [str(root)],
                "provider_base_urls": [model.url],
                "postgres_dsns": [pg_dsn],
                "max_schedules": 4,
                "max_queued_jobs": 4,
                "min_schedule_interval_ms": 1000,
                "workflow": {},
            }
            tenants = {"acme": tenant(ACME, **allow), "beta": tenant(BETA, **allow)}
            async with serving(
                tenants, extra={"worker": {"dsn": {"value": pg_dsn}, "poll_interval_ms": 250}}
            ) as proc:
                async with proc.connect(ACME) as one, proc.connect(BETA) as two:

                    def cfg(name: str) -> Any:
                        return fake_config(
                            model, namespace=name, stores={"type": "postgres", "dsn": pg_dsn}
                        )

                    acme = await one.open_agent(cfg("acme"))
                    beta = await two.open_agent(cfg("beta"))
                    run = await acme.run("shared-session-name", "go")
                    await run.result()
                    schedule = await acme.create_schedule("go", interval_ms=120_000, id="mine")
                    job = await acme.enqueue("s2", "go")

                    from linch_rust import run_workflow

                    async def flow(wf: Any) -> str:
                        return await wf.step("one", lambda: "acme-secret-result")

                    assert await run_workflow(acme, flow, run_id="wf-1") == "acme-secret-result"

                    probes: dict[str, Any] = {}
                    for label, probe in (
                        ("events", beta.events(run.id)),
                        ("resume", _result(beta.resume(run.id))),
                        ("job", beta.job(job["job_id"])),
                    ):
                        try:
                            await probe
                            probes[label] = "LEAK"
                        except (RunNotFound, JobNotFound) as error:
                            probes[label] = type(error).__name__
                        except Exception as error:  # record whatever else it is
                            probes[label] = f"{type(error).__name__}: {str(error)[:120]}"
                    probes["schedules"] = await beta.schedules()
                    probes["delete_schedule"] = await beta.delete_schedule("mine")
                    probes["jobs"] = await beta.jobs()

                    # the same workflow id under beta is another workflow, not acme's journal
                    async def beta_flow(wf: Any) -> str:
                        return await wf.step("one", lambda: "beta-own-result")

                    probes["workflow_same_id"] = await run_workflow(beta, beta_flow, run_id="wf-1")
                    # a session guess: beta's run in a session of the same name does not see acme's
                    beta_run = await beta.run("shared-session-name", "go")
                    await beta_run.result()
                    probes["acme_events_after"] = len(await acme.events(run.id))
                    record(AREA, "6-isolation", **{k: v for k, v in probes.items()})
                    assert probes["events"] == "RunNotFound" and probes["resume"] == "RunNotFound"
                    assert probes["job"] == "JobNotFound"
                    assert probes["schedules"] == [] and probes["delete_schedule"] is False
                    assert probes["jobs"] == []
                    assert probes["workflow_same_id"] == "beta-own-result"
                    assert [s["id"] for s in await acme.schedules()] == ["mine"]
                    assert schedule["id"] == "mine"
    finally:
        await model.stop()


async def _result(awaitable: Any) -> Any:
    return await (await awaitable).result()


@needs_server
async def test_tool_cache_hit_miss_clear_and_no_sharing_across_principals() -> None:
    calls: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        """Look a value up."""
        calls.append(q)
        return f"value-{q}-{len(calls)}"

    @tool(scope="write")
    async def touch(what: str) -> str:
        """Change something."""
        return f"touched-{what}"

    counter = {"n": 0}

    def per_run(plan: list[tuple[str, dict[str, object]]]) -> Any:
        def handler(seen: Any) -> Any:
            if seen.body["messages"][-1]["role"] == "tool":
                return ("text", "done")
            counter["n"] += 1
            return ("tools", [plan[(counter["n"] - 1) % len(plan)]])

        return handler

    plan: list[tuple[str, dict[str, object]]] = [
        ("lookup", {"q": "a"}),  # alice s1: miss
        ("lookup", {"q": "a"}),  # alice s2: hit (namespace scope, same principal)
        ("lookup", {"q": "a"}),  # bob s3: miss (another principal, #162)
        ("lookup", {"q": "a"}),  # bob s4: hit
        ("touch", {"what": "x"}),  # alice s5: write tool clears
        ("lookup", {"q": "a"}),  # alice s6: miss again
    ]
    model = await FakeModel(per_run(plan)).start()
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "provider_base_urls": [model.url],
                "workspace_roots": [str(root)],
                "permission_modes": ["default", "skip_dangerous"],
                "tool_cache": True,
                "tool_cache_shared": True,
            }
            async with serving({"acme": tenant(ACME, **allow)}) as proc:
                async with proc.connect(ACME) as server:
                    config = fake_config(
                        model,
                        namespace="acme",
                        spec={
                            "tool_cache": {
                                "tools": ["lookup"],
                                "scope": "namespace",
                                "ttl_ms": 60000,
                            },
                            "redaction": {"rules": [{"pattern": "zzzz-never-matches"}]},
                        },
                        permissions={"mode": "default", "project_root": str(root)},
                    )
                    agent = await server.open_agent(config, tools=[lookup, touch])
                    alice = {"tenant": "acme", "user": "alice"}
                    bob = {"tenant": "acme", "user": "bob"}
                    hits = []
                    for session, principal in (
                        ("s1", alice),
                        ("s2", alice),
                        ("s3", bob),
                        ("s4", bob),
                        ("s5", alice),
                        ("s6", alice),
                    ):
                        run = await agent.run(session, "go", principal=principal)
                        envelopes = await finish(agent, run)
                        name = "touch" if session == "s5" else "lookup"
                        end = ends(envelopes, name)[0]
                        hits.append((session, name, end.cache_hit, text_of(end)))
                    record(AREA, "6-cache", sequence=hits, tool_runs=list(calls))
                    flags = [h[2] for h in hits]
                    assert flags == [False, True, False, True, False, False], hits
                    assert len(calls) == 3
    finally:
        await model.stop()
