"""Live toolsec on Postgres: a tool cache shared by namespace is not shared across tenants, an error past
admission leaks no DSN, and the worker's `LINCH_LOG=debug` output holds no provider header value.

Expectations are in `toolsec/expectations.md` section 6 (cache, error text) and 7.7.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from live_sched_support import pg_dsn
from ts_support import (
    FakeModel,
    ends,
    fake_config,
    finish,
    needs_server,
    pytestmark,
    record,
    server_env,
    serving,
    tenant,
    workspace,
)

from linch_rust import LinchError, NotAllowed, RpcError, tool

__all__ = ["pg_dsn", "pytestmark"]

AREA = "tenants_pg"
ACME, BETA = "tok-acme-pg-0001", "tok-beta-pg-0001"


@needs_server
async def test_tool_cache_entries_never_cross_tenants_on_one_postgres(pg_dsn: str) -> None:
    calls: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        """Look a value up."""
        calls.append(q)
        return f"value-{q}-{len(calls)}"

    def handler(seen: Any) -> Any:
        if seen.body["messages"][-1]["role"] == "tool":
            return ("text", "done")
        return ("tools", [("lookup", {"q": "same-input"})])

    model = await FakeModel(handler).start()
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "provider_base_urls": [model.url],
                "workspace_roots": [str(root)],
                "postgres_dsns": [pg_dsn],
                "tool_cache": True,
                "tool_cache_shared": True,
            }
            tenants = {"acme": tenant(ACME, **allow), "beta": tenant(BETA, **allow)}
            async with serving(tenants) as proc:
                flags = []
                async with proc.connect(ACME) as one, proc.connect(BETA) as two:
                    for server, namespace, user in (
                        (one, "acme", "ann"),
                        (two, "beta", "bo"),
                        (one, "acme", "ann"),
                        (two, "beta", "bo"),
                    ):
                        config = fake_config(
                            model,
                            namespace=namespace,
                            stores={"type": "postgres", "dsn": pg_dsn},
                            permissions={"mode": "default", "project_root": str(root)},
                            workspace=str(root),
                            bash=False,
                            spec={
                                "tool_cache": {
                                    "tools": ["lookup"],
                                    "scope": "namespace",
                                    "ttl_ms": 60000,
                                },
                                "redaction": {"rules": [{"pattern": "zzzz-never-matches"}]},
                            },
                        )
                        agent = await server.open_agent(config, tools=[lookup])
                        principal = {"tenant": namespace, "user": user}
                        run = await agent.run(
                            f"s-{len(flags)}", f"{namespace}-prompt", principal=principal
                        )
                        end = ends(await finish(agent, run), "lookup")[0]
                        flags.append((namespace, end.cache_hit))
                        await agent.close()
    finally:
        await model.stop()
    record(AREA, "6-cache-cross-tenant", flags=flags, tool_runs=len(calls))
    assert flags == [("acme", False), ("beta", False), ("acme", True), ("beta", True)], flags
    assert len(calls) == 2, "beta's identical call must run the tool, not read acme's entry"


@needs_server
async def test_an_error_past_admission_leaks_no_dsn_or_path(pg_dsn: str) -> None:
    password = "pw-" + secrets.token_hex(6)
    unreachable = f"postgres://svcuser:{password}@127.0.0.1:1/private_db_name"
    model = await FakeModel(lambda seen: ("http", 500, b"upstream boom")).start()
    texts: list[str] = []
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "provider_base_urls": [model.url],
                "workspace_roots": [str(root)],
                "postgres_dsns": [unreachable, pg_dsn],
            }
            async with serving({"acme": tenant(ACME, **allow)}) as proc:
                async with proc.connect(ACME) as server:
                    config = fake_config(
                        model,
                        namespace="acme",
                        stores={"type": "postgres", "dsn": unreachable},
                    )
                    try:
                        await server.open_agent(config)
                        texts.append("OPENED")
                    except (RpcError, LinchError) as error:
                        texts.append(f"{type(error).__name__}: {error}")
                    # a run that fails at the provider, workspace configured
                    config = fake_config(
                        model,
                        namespace="acme",
                        workspace=str(root),
                        permissions={"mode": "default", "project_root": str(root)},
                    )
                    agent = await server.open_agent(config)
                    envelopes = await finish(agent, await agent.run("s1", "go"))
                    texts += [
                        repr(e.event) for e in envelopes if e.event.type in {"error", "result"}
                    ]
                    try:
                        await server.open_agent(
                            fake_config(model, namespace="acme", workspace="/tmp")
                        )
                    except NotAllowed as error:
                        texts.append(str(error))
                server_dir = str(proc.dir)
    finally:
        await model.stop()
    joined = "\n".join(texts)
    record(
        AREA,
        "6-error-text-past-admission",
        texts=[t[:300] for t in texts],
        host_port_of_the_allowed_dsn_in_text="127.0.0.1:1" in joined,
    )
    assert texts[0] != "OPENED", "an unreachable store opened"
    for needle in (password, "svcuser", "private_db_name", server_dir, ACME):
        assert needle not in joined, f"{needle!r} appears in a tenant-visible error"


@needs_server
async def test_debug_log_of_a_stored_agent_and_worker_has_no_header_values(pg_dsn: str) -> None:
    values = {
        "x-gateway-token": secrets.token_hex(3),
        "x-trace-id": secrets.token_hex(3),
        "x-long-trace": secrets.token_hex(8),
    }
    model = await FakeModel(
        lambda seen: ("http", 400, json.dumps({"error": {"message": "nope"}}).encode())
    ).start()
    try:
        with workspace():
            allow: dict[str, Any] = {
                "provider_base_urls": [model.url],
                "postgres_dsns": [pg_dsn],
                "provider_request": True,
                "max_queued_jobs": 4,
                "max_schedules": 2,
                "min_schedule_interval_ms": 1000,
            }
            worker = {"dsn": {"value": pg_dsn}, "poll_interval_ms": 200}
            async with serving(
                {"acme": tenant(ACME, **allow)},
                env=server_env(LINCH_LOG="debug"),
                extra={"worker": worker},
            ) as proc:
                async with proc.connect(ACME) as server:
                    config = fake_config(
                        model, namespace="acme", stores={"type": "postgres", "dsn": pg_dsn}
                    )
                    config["provider"] = {
                        **config["provider"],
                        "request": {"default_headers": dict(values)},
                    }
                    agent = await server.open_agent(config)
                    job = await agent.enqueue("s1", "go")
                    beat = await agent.create_schedule("go", interval_ms=1000)
                    try:
                        await agent.wait_job(job["job_id"], timeout=30)
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                    await agent.delete_schedule(beat["id"])
                log = proc.log
    finally:
        await model.stop()
    wire = [r.headers for r in model.requests]
    seen = {name: value in log for name, value in values.items()}
    record(
        AREA,
        "7.7-debug-log-worker",
        log_bytes=len(log),
        worker_lines=log.count("linch_worker"),
        values_in_log=seen,
        gateway_requests=len(wire),
        gateway_got_values=bool(wire) and all(wire[0].get(n) == v for n, v in values.items()),
    )
    assert wire and all(wire[0].get(n) == v for n, v in values.items()), (
        "positive control: the worker's run must send the stored headers"
    )
    assert len(log) > 5000 and "linch_worker" in log, "the debug log is too thin to prove anything"
    for name, value in values.items():
        assert value not in log, f"{name} value is in the LINCH_LOG=debug output"
