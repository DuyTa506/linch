"""Live e2e (area long): file memory and built-in long-term memory (K1-K5 of
`expectations/long.md`).

Postgres stores (a schema per test), vLLM through the body-logging tee. K5 seeds
the scope with the scripted provider (the same namespace, principal and memory
config) and times recall with the server's own envelope timestamps.
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest
from e2e_long_support import Tee, dump, load, msg_text
from live_core_support import ONLY_VLLM, Live, live_server, make_config, needs_vllm
from live_sched_support import pg_dsn, query

from linch_rust import (
    ContextBuildEvent,
    ResultEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from linch_rust.scripted import MODEL as SCRIPT_MODEL
from linch_rust.scripted import text_turn, tool_turn

__all__ = ["pg_dsn"]

pytestmark = [pytest.mark.live, needs_vllm]

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
ADA = {"tenant": "acme", "user": "ada"}
BOB = {"tenant": "acme", "user": "bob"}


def vllm_config(tee: Tee, dsn: str, namespace: str, **top: Any) -> Any:
    config = make_config(
        LIVE,
        LIVE.spec(max_turns=8, max_output_tokens=300),
        stores={"type": "postgres", "dsn": dsn},
        permissions={"mode": "skip_dangerous", "project_root": "/"},
        **top,
    )
    config["namespace"] = namespace
    config["provider"] = {"id": "vllm", "base_url": tee.url}
    return config


def results_of(events: list[Any], tool: str) -> list[tuple[str, bool]]:
    return [
        ("".join(p.get("text", "") for p in e.result.get("content", [])), bool(e.is_error))
        for e in events
        if isinstance(e, ToolCallEndEvent) and e.tool_name == tool
    ]


async def ask(agent: Any, session: str, prompt: str, **kw: Any) -> tuple[Any, list[Any]]:
    run = await agent.run(session, prompt, **kw)
    result = await run.result()
    assert isinstance(result, ResultEvent), result
    return result, [e.event for e in await agent.events(run.id)]


@pytest.mark.parametrize("rep", [1, 2, 3])
async def test_file_memory_written_in_one_session_is_read_in_another(pg_dsn: str, rep: int) -> None:
    """K1: a plain request to remember, no mention of paths; three repetitions."""
    tee = Tee(f"memory_file_{rep}")
    out: dict[str, Any] = {}
    config = vllm_config(tee, pg_dsn, "e2e-filemem", filesystem={"memory": True})
    try:
        async with live_server() as server:
            agent = await server.open_agent(config)
            _, events1 = await ask(
                agent,
                "A",
                "Remember this for later conversations: my favourite colour is teal.",
            )
            out["run1_calls"] = [e.tool_name for e in events1 if isinstance(e, ToolCallStartEvent)]
            n_before = len(tee.chat())
            result2, events2 = await ask(
                agent, "B", "What is my favourite colour? Answer with one word."
            )
            out["run2_calls"] = [e.tool_name for e in events2 if isinstance(e, ToolCallStartEvent)]
            out["run2_answer"] = result2.final_text
    finally:
        tee.close()
    files = query(
        pg_dsn,
        "SELECT path, content FROM linch_files WHERE namespace = %s ORDER BY path",
        "e2e-filemem",
    )
    out["files"] = [list(r) for r in files]
    first_request_run2 = tee.chat()[n_before]
    tail = msg_text(first_request_run2["body"]["messages"][-1])
    out["run2_first_request_tail"] = tail[-900:]
    out["index_fenced_in_tail"] = "<memory-index>" in tail and "teal" in tail.lower()
    out["instruction_names_dir"] = "/memories/" in tail
    dump(f"memory_file_{rep}", out)
    assert any(r[0] == "/memories/MEMORY.md" and "teal" in r[1].lower() for r in files), files
    assert out["index_fenced_in_tail"] and out["instruction_names_dir"], tail[-500:]
    assert "teal" in (out["run2_answer"] or "").lower()


async def test_long_term_memory_save_recall_scopes_and_caps(pg_dsn: str) -> None:
    """K2 K3 K4."""
    tee = Tee("memory_ltm")
    out: dict[str, Any] = {}
    ns = "e2e-ltm"
    mem = {"max_memories": 6, "max_memories_per_scope": 2}
    try:
        async with live_server() as server:
            plain = await server.open_agent(vllm_config(tee, pg_dsn, ns, memory=dict(mem)))
            recalling = await server.open_agent(
                vllm_config(tee, pg_dsn, ns, memory={**mem, "recall": {"k": 5}})
            )
            # K2: ada saves; a new session finds it by words, with a tool and through recall.
            _, ev = await ask(
                plain,
                "s1",
                "Use MemorySave to save this fact: the staging password rotation day is Thursday. "
                "Then reply ok.",
                principal=ADA,
            )
            out["ada_save"] = results_of(ev, "MemorySave")
            _, ev = await ask(
                plain,
                "s2",
                "Call MemorySearch with query 'rotation' and tell me the day.",
                principal=ADA,
            )
            out["ada_search_new_session"] = results_of(ev, "MemorySearch")
            n = len(tee.chat())
            result, ev = await ask(
                recalling,
                "s3",
                "What is the staging password rotation day? One word.",
                principal=ADA,
            )
            first = tee.chat()[n]
            tail = msg_text(first["body"]["messages"][-1])
            out["recall_in_tail"] = '<memory-data untrusted="true">' in tail and "Thursday" in tail
            out["recall_tail"] = tail[-600:]
            out["recall_answer"] = result.final_text
            out["recall_tool_calls"] = [
                e.tool_name for e in ev if isinstance(e, ToolCallStartEvent)
            ]
            out["recall_metadata"] = [e.metadata for e in ev if isinstance(e, ContextBuildEvent)][
                :1
            ]
            # K3: bob sees none of ada's memory; no principal means no memory.
            _, ev = await ask(
                plain, "s4", "Call MemorySearch with query 'rotation'.", principal=BOB
            )
            out["bob_search"] = results_of(ev, "MemorySearch")
            _, ev = await ask(
                plain, "s5", "Use MemorySave to save: no principal fact. Then reply ok."
            )
            out["no_principal_save"] = results_of(ev, "MemorySave")
            _, ev = await ask(
                plain, "s6", "Use MemorySave to save: bob likes jazz. Then reply ok.", principal=BOB
            )
            out["bob_save"] = results_of(ev, "MemorySave")
            _, ev = await ask(
                plain, "s7", "Call MemorySearch with query 'rotation jazz'.", principal=ADA
            )
            out["ada_search_after_bob"] = results_of(ev, "MemorySearch")
            # K4: ada's second save fits (2 per scope), the third is refused.
            _, ev = await ask(
                plain,
                "s8",
                "Make three MemorySave calls, one after another, saving 'fact two: ada owns a "
                "cat', "
                "then 'fact three: ada owns a dog', then 'fact four: ada owns a fish'. Then reply "
                "ok.",
                principal=ADA,
            )
            out["ada_cap_saves"] = results_of(ev, "MemorySave")
            out["ada_cap_calls"] = [e.tool_name for e in ev if isinstance(e, ToolCallStartEvent)]
            out["ada_cap_deletes"] = results_of(ev, "MemoryDelete")
    finally:
        tee.close()
    rows = query(
        pg_dsn,
        "SELECT scope, count(*) FROM linch_memories WHERE namespace = %s GROUP BY scope ORDER BY "
        "scope",
        ns,
    )
    out["rows_by_scope"] = [list(r) for r in rows]
    out["load"] = load()
    dump("memory_ltm", out)
    assert out["ada_save"] and not out["ada_save"][0][1], out["ada_save"]
    assert out["ada_search_new_session"] and "Thursday" in out["ada_search_new_session"][0][0]
    assert out["recall_in_tail"], out["recall_tail"]
    assert out["bob_search"] and "Thursday" not in out["bob_search"][0][0]
    assert out["no_principal_save"] and out["no_principal_save"][0][1], out["no_principal_save"]
    assert (
        "not_allowed" in out["no_principal_save"][0][0]
        or "principal" in out["no_principal_save"][0][0].lower()
    )
    assert out["ada_search_after_bob"] and "jazz" not in out["ada_search_after_bob"][0][0]
    assert len(rows) == 2 and all(r[1] <= 2 for r in rows), rows
    refused = [r for r in out["ada_cap_saves"] if r[1]]
    assert refused and "limit" in refused[0][0].lower(), out["ada_cap_saves"]


SEED = 200


async def test_recall_latency_with_200_memories_in_the_scope(pg_dsn: str) -> None:
    """K5: seed with the scripted provider, then time 10 recalls and 10 MemorySearch calls."""
    ns = "e2e-recall"
    memory = {"max_memories": 1000, "max_memories_per_scope": 500}
    out: dict[str, Any] = {"load": load()}
    turns: list[dict[str, Any]] = []
    for i in range(SEED):
        words = (
            ("kestrel " if i % 2 == 0 else "")
            + ("deployment " if i % 3 != 0 else "")
            + ("pipeline " if i % 5 == 0 else "")
        )
        turns.append(
            tool_turn(
                f"seed-{i}",
                "MemorySave",
                {
                    "content": f"note {i}: {words}uses runner {i} for stage {i % 7}",
                    "tags": ["seed"],
                },
            )
        )
    turns.append(text_turn("seeded"))
    seed_config = {
        "namespace": ns,
        "spec": {"model": SCRIPT_MODEL, "max_turns": SEED + 10},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": pg_dsn},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "memory": memory,
    }
    tee = Tee("memory_recall")
    try:
        async with live_server() as server:
            seeder = await server.open_agent(seed_config)  # type: ignore[arg-type]
            result, _ = await ask(seeder, "seed", "seed the memory", principal=ADA)
            out["seed_result"] = result.final_text
            rows = query(
                pg_dsn,
                "SELECT scope, count(*) FROM linch_memories WHERE namespace = %s GROUP BY scope",
                ns,
            )
            out["rows_after_seed"] = [list(r) for r in rows]
            # The vLLM agent must see the same scope: save one memory through it as ada.
            recalling = await server.open_agent(
                vllm_config(tee, pg_dsn, ns, memory={**memory, "recall": {"k": 5}})
            )
            plain = await server.open_agent(vllm_config(tee, pg_dsn, ns, memory=dict(memory)))
            baseline = await server.open_agent(vllm_config(tee, pg_dsn, "e2e-recall-off"))
            _, ev = await ask(
                plain,
                "scope-check",
                "Use MemorySave to save: kestrel deployment pipeline owner is ada. Then reply ok.",
                principal=ADA,
            )
            out["scope_check_save"] = results_of(ev, "MemorySave")
            rows2 = query(
                pg_dsn,
                "SELECT scope, count(*) FROM linch_memories WHERE namespace = %s GROUP BY scope",
                ns,
            )
            out["rows_after_vllm_save"] = [list(r) for r in rows2]

            question = "Which runner does the kestrel deployment pipeline use?"
            gaps: list[float] = []
            gaps_off: list[float] = []
            hits: list[int] = []
            for i in range(10):
                run = await recalling.run(f"r{i}", question, principal=ADA)  # pyright: ignore[reportArgumentType]
                await run.result()
                envs = await recalling.events(run.id)
                for j, env in enumerate(envs):
                    if isinstance(env.event, ContextBuildEvent):
                        gaps.append(env.ts_ms - envs[j - 1].ts_ms)
                        ids = (env.event.metadata.get("memory") or {}).get("ids") or []
                        hits.append(len(ids))
                        break
                run = await baseline.run(f"b{i}", question, principal=ADA)  # pyright: ignore[reportArgumentType]
                await run.result()
                envs = await baseline.events(run.id)
                for j, env in enumerate(envs):
                    if isinstance(env.event, ContextBuildEvent):
                        gaps_off.append(env.ts_ms - envs[j - 1].ts_ms)
                        break
            out["recall_gap_ms"] = gaps
            out["recall_off_gap_ms"] = gaps_off
            out["recall_hits"] = hits
            searches: list[float] = []
            for i in range(10):
                run = await plain.run(
                    f"q{i}",
                    "Call MemorySearch with query 'kestrel deployment pipeline' and limit 5, then "
                    "reply ok.",
                    principal=ADA,  # pyright: ignore[reportArgumentType]
                )
                await run.result()
                envs = await plain.events(run.id)
                start = next(
                    e
                    for e in envs
                    if isinstance(e.event, ToolCallStartEvent)
                    and e.event.tool_name == "MemorySearch"
                )
                end = next(
                    e
                    for e in envs
                    if isinstance(e.event, ToolCallEndEvent) and e.event.tool_name == "MemorySearch"
                )
                searches.append(end.ts_ms - start.ts_ms)
            out["memory_search_tool_ms"] = searches
    finally:
        tee.close()
        out["p50_recall_gap_ms"] = statistics.median(out.get("recall_gap_ms", [0]))
        out["p50_off_gap_ms"] = statistics.median(out.get("recall_off_gap_ms", [0]))
        out["p50_search_tool_ms"] = statistics.median(out.get("memory_search_tool_ms", [0]))
        dump("memory_recall_latency", out)
    assert out["rows_after_seed"] and out["rows_after_seed"][0][1] == SEED
    assert len(out["rows_after_vllm_save"]) == 1, (
        "the vLLM agent saved into the scripted agent's scope"
    )
    assert all(h > 0 for h in out["recall_hits"]), out["recall_hits"]
    print(
        f"recall p50 gap {out['p50_recall_gap_ms']} ms vs off {out['p50_off_gap_ms']} ms; "
        f"MemorySearch p50 {out['p50_search_tool_ms']} ms"
    )
    assert out["p50_recall_gap_ms"] < 300
