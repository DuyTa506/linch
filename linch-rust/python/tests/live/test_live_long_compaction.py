"""Live e2e (area long): compaction against vLLM (expectations C1-C7, M3).

The provider is reached through the body-logging tee, so the exact requests of
the primary calls and of the summary calls are compared. Raw data goes to
`$E2E_RESULTS/compaction_*.json` (requests are summarised, the checkpoint and
the summary instruction are kept whole for the first repetition).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from e2e_long_support import Tee, dump, load, msg_text
from live_core_support import ONLY_VLLM, Live, live_server, make_config, needs_vllm

from linch_rust import CompactionEvent, ResultEvent, tool

pytestmark = [pytest.mark.live, needs_vllm]

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
INTRO = "The conversation to summarize ends above."
UPDATE = "begins with an earlier checkpoint"
VERBATIM = "[user message, verbatim]"
SUMMARY_TAG = "<summary of earlier conversation>"
CODE = "PELICAN-7421"
REGION, ORDER = "EU-WEST-9", "ORD-55231"


def filler(n: int, words: int = 220) -> str:
    body = " ".join(f"log{n}-{i}:ok" for i in range(words))
    return f"FYI, here is log excerpt {n}, no action needed, just reply ok: {body}."


@tool(scope="read")
async def lookup(key: str) -> str:
    """Look up a record by key."""
    return f"Record {key}: the deployment region is {REGION}; the order id is {ORDER}."


def agent_config(
    tee: Tee,
    *,
    threshold: int | None = 2500,
    keep_recent: int = 2,
    keep_user_tokens: int | None = None,
    window: int | None = None,
    compaction_output: int = 600,
    **spec_extra: Any,
) -> Any:
    compaction: dict[str, Any] = {
        "keep_recent_turns": keep_recent,
        "max_output_tokens": compaction_output,
    }
    if threshold is not None:
        compaction["threshold_tokens"] = threshold
    if keep_user_tokens is not None:
        compaction["keep_user_tokens"] = keep_user_tokens
    config = make_config(
        LIVE, LIVE.spec(max_output_tokens=200, compaction=compaction, **spec_extra)
    )
    provider: dict[str, Any] = {"id": "vllm", "base_url": tee.url}
    if window is not None:
        provider["context_window"] = window
    config["provider"] = provider
    return config


async def converse(agent: Any, fillers: int, words: int = 220) -> dict[str, Any]:
    """Early facts (a user word, a tool result), `fillers` log turns, then the recall question."""
    prompts = [
        f"Hello. Our project code word is {CODE}. Remember it. Reply ok.",
        "Call the lookup tool with key 'deploy' and tell me in one line what it says.",
        *(filler(n, words) for n in range(1, fillers + 1)),
    ]
    run_ids: list[str] = []
    compactions: list[tuple[int, CompactionEvent]] = []
    for i, text in enumerate(prompts):
        run = await agent.run("long", text)
        result = await run.result()
        assert isinstance(result, ResultEvent), result
        run_ids.append(run.id)
        for env in await agent.events(run.id):
            if isinstance(env.event, CompactionEvent):
                compactions.append((i, env.event))
    question = await agent.run(
        "long",
        "What are the project code word, the deployment region and the order id? "
        "Answer with the three values only.",
    )
    answer = await question.result()
    assert isinstance(answer, ResultEvent)
    for env in await agent.events(question.id):
        if isinstance(env.event, CompactionEvent):
            compactions.append((len(prompts), env.event))
    return {"run_ids": run_ids, "compactions": compactions, "answer": answer.final_text or ""}


def is_summary_request(rec: dict[str, Any]) -> bool:
    last = rec["body"]["messages"][-1]
    return last["role"] == "user" and msg_text(last).startswith(INTRO)


def view_of(rec: dict[str, Any]) -> list[str]:
    return [f"{m['role']}:{len(msg_text(m))}" for m in rec["body"]["messages"]]


def analyse(tee: Tee) -> dict[str, Any]:
    chats = tee.chat()
    out: dict[str, Any] = {
        "requests": len(chats),
        "summaries": [],
        "all": [
            {
                "i": i,
                "kind": "summary" if is_summary_request(r) else "primary",
                "messages": len(r["body"]["messages"]),
                "prompt_tokens": r.get("prompt_tokens"),
                "cached_tokens": r.get("cached_tokens"),
                "completion_tokens": r.get("completion_tokens"),
                "latency_s": r.get("latency_s"),
            }
            for i, r in enumerate(chats)
        ],
    }
    for idx, rec in enumerate(chats):
        if not is_summary_request(rec):
            continue
        prev = next((r for r in reversed(chats[:idx]) if not is_summary_request(r)), None)
        s_msgs = rec["body"]["messages"]
        info: dict[str, Any] = {
            "index": idx,
            "status": rec.get("status"),
            "messages": len(s_msgs),
            "prompt_tokens": rec.get("prompt_tokens"),
            "cached_tokens": rec.get("cached_tokens"),
            "completion_tokens": rec.get("completion_tokens"),
            "latency_s": rec.get("latency_s"),
            "instruction_chars": len(msg_text(s_msgs[-1])),
            "updates_checkpoint": UPDATE in msg_text(s_msgs[-1]),
            "tools_equal_prev": prev is not None
            and prev["body"].get("tools") == rec["body"].get("tools"),
            "system_equal_prev": prev is not None and prev["body"]["messages"][0] == s_msgs[0],
        }
        if prev is not None:
            p_msgs = prev["body"]["messages"]
            info["prev_messages"] = len(p_msgs)
            info["prev_prompt_tokens"] = prev.get("prompt_tokens")
            info["prompt_delta_tokens"] = (rec.get("prompt_tokens") or 0) - (
                prev.get("prompt_tokens") or 0
            )
            info["prev_is_prefix"] = s_msgs[: len(p_msgs)] == p_msgs
            info["extra_messages_between"] = len(s_msgs) - 1 - len(p_msgs)
            info["trailing_messages_after_prefix"] = [
                f"{m['role']}:{msg_text(m)[:50]!r}" for m in s_msgs[len(p_msgs) : -1]
            ]
        out["summaries"].append(info)
        nxt = chats[idx + 1] if idx + 1 < len(chats) else None
        if nxt is not None:
            n_msgs = nxt["body"]["messages"]
            cps = [m for m in n_msgs if SUMMARY_TAG in msg_text(m)]
            info["next_request_summary_messages"] = len(cps)
            info["next_request_summary_count_in_text"] = sum(
                msg_text(m).count(SUMMARY_TAG) for m in n_msgs
            )
            info["next_request_verbatim_blocks"] = sum(msg_text(m).count(VERBATIM) for m in n_msgs)
            info["next_request_messages"] = len(n_msgs)
            info["next_request_prompt_tokens"] = nxt.get("prompt_tokens")
            info["checkpoint_text"] = msg_text(cps[0]) if cps else None
    return out


async def run_scenario(
    label: str, config_kwargs: dict[str, Any], fillers: int, words: int = 220
) -> tuple[dict[str, Any], dict[str, Any], Tee]:
    tee = Tee(label)
    try:
        async with live_server() as server:
            agent = await server.open_agent(agent_config(tee, **config_kwargs), tools=[lookup])
            data = await converse(agent, fillers, words)
    finally:
        tee.close()
    report = analyse(tee)
    report["load"] = load()
    report["compactions"] = [
        {
            "at_prompt": i,
            "strategy": c.strategy,
            "forced": c.forced,
            "messages_before": c.messages_before,
            "messages_after": c.messages_after,
            "tokens_before": c.tokens_before,
            "tokens_after": c.tokens_after,
            "duration_ms": c.duration_ms,
            "rejected": getattr(c, "rejected", None),
        }
        for i, c in data["compactions"]
    ]
    report["answer"] = data["answer"]
    report["recall"] = {
        "code": CODE in data["answer"],
        "region": REGION in data["answer"],
        "order": ORDER in data["answer"],
    }
    return data, report, tee


@tool(scope="read")
async def lookup_big(key: str) -> str:
    """Look up a bulky record by key."""
    return f"Record {key}: " + " ".join(f"{key}-field{i}=value{i}" for i in range(150))


RULE = "ZEBRA-SEVEN-RULE"


async def test_the_checkpoint_carries_early_short_user_messages_verbatim() -> None:
    """C5: user messages are short, tool results bulky: every folded user message fits the "
    "budget."""
    tee = Tee("compaction_verbatim")
    try:
        async with live_server() as server:
            agent = await server.open_agent(
                agent_config(tee, threshold=3000, keep_recent=2), tools=[lookup_big]
            )
            prompts = [
                "Hello. Reply ok.",
                f"Standing rule: always sign off with the phrase {RULE}. Reply ok.",
                *(
                    f"Call lookup_big with key 'k{n}', then reply with one word."
                    for n in range(1, 7)
                ),
            ]
            for text in prompts:
                result = await (await agent.run("v", text)).result()
                assert isinstance(result, ResultEvent)
    finally:
        tee.close()
    report = analyse(tee)
    dump("compaction_verbatim", report)
    summaries = [s for s in report["summaries"] if s.get("checkpoint_text")]
    assert summaries, report
    first = summaries[0]
    print("verbatim blocks:", first["next_request_verbatim_blocks"])
    assert (
        f"{VERBATIM}\nStanding rule: always sign off with the phrase {RULE}"
        in first["checkpoint_text"]
    )


@pytest.mark.parametrize("rep", [1, 2, 3])
async def test_summary_request_checkpoint_recall_and_second_compaction(rep: int) -> None:
    """C1 C2 C3 C4 C5 C6 (default keep_user_tokens)."""
    _, report, _ = await run_scenario(f"compaction_default_{rep}", {}, fillers=12)
    dump(f"compaction_default_{rep}", report)
    summaries = report["summaries"]
    comps = [c for c in report["compactions"] if c["strategy"] == "summary" and not c["rejected"]]
    assert comps and len(comps) >= 2, report["compactions"]
    for c in comps:
        assert (
            c["tokens_before"] > c["tokens_after"] and c["messages_after"] < c["messages_before"]
        ), c
        assert c["duration_ms"] > 0
    first = summaries[0]
    # C1: system and tools identical, the previous primary request is a prefix, one trailing
    # instruction.
    assert first["status"] == 200
    assert first["system_equal_prev"] and first["tools_equal_prev"], first
    assert first["prev_is_prefix"], first
    # C5: verbatim user blocks, the first message included, in the next request.
    assert first["next_request_verbatim_blocks"] >= 1, first
    assert CODE in first["checkpoint_text"]
    # C6: the second summary updates the first; one summary message in the view, never nested.
    second = summaries[1]
    assert second["updates_checkpoint"], second
    assert second["next_request_summary_messages"] == 1
    assert second["next_request_summary_count_in_text"] == 1, "no nested summaries"
    print(
        f"rep {rep}: recall code={report['recall']['code']} region={report['recall']['region']} "
        f"order={report['recall']['order']}; delta tokens {first.get('prompt_delta_tokens')} "
        f"cached {first['cached_tokens']}/{first['prompt_tokens']}"
    )
    assert report["recall"]["region"] and report["recall"]["order"], report["answer"]


async def test_keep_user_tokens_zero_carries_no_verbatim_blocks() -> None:
    """C5: 0 turns the verbatim section off; the summary request is the same shape."""
    _, report, _ = await run_scenario("compaction_keep_user_0", {"keep_user_tokens": 0}, fillers=12)
    dump("compaction_keep_user_0", report)
    summaries = report["summaries"]
    assert summaries
    assert all(s["next_request_verbatim_blocks"] == 0 for s in summaries), summaries
    first = summaries[0]
    assert first["prev_is_prefix"] and first["system_equal_prev"]
    print(f"keep_user_tokens=0 recall: {report['recall']} answer={report['answer']!r}")


async def test_compaction_without_a_threshold_follows_the_context_window() -> None:
    """C7: no `threshold_tokens`; the provider window is 6000 tokens."""
    _, report, _ = await run_scenario(
        "compaction_window",
        {"threshold": None, "window": 6000, "compaction_output": 600},
        fillers=28,
        words=100,
    )
    dump("compaction_window", report)
    comps = [c for c in report["compactions"] if c["strategy"] == "summary" and not c["rejected"]]
    assert comps, report["compactions"]
    # Due when estimate + reserve >= 0.8 * 6000 = 4800: reserve 600 -> 4200, reserve 200 -> 4600.
    print("window path tokens_before:", [c["tokens_before"] for c in comps])
    assert all(4000 <= c["tokens_before"] <= 5300 for c in comps), comps


@pytest.mark.parametrize("threshold", [2000, 6000, 14000])
async def test_time_per_compaction_by_history_size(threshold: int) -> None:
    """M3 (run on the release binary): duration_ms of the first summary by view size."""
    fillers = max(6, threshold // 350 + 4)
    _, report, _ = await run_scenario(
        f"compaction_time_{threshold}", {"threshold": threshold}, fillers=fillers
    )
    dump(f"compaction_time_{threshold}", report)
    comps = [c for c in report["compactions"] if c["strategy"] == "summary" and not c["rejected"]]
    assert comps
    print(
        f"threshold {threshold}: "
        + json.dumps([(c["tokens_before"], c["tokens_after"], c["duration_ms"]) for c in comps])
    )
