"""Live e2e (area long): context budget and system sections (B1-B3 of "
"`expectations/long.md`)."""

from __future__ import annotations

from typing import Any

import pytest
from e2e_long_support import Tee, dump, msg_text
from live_core_support import LIVE_KIND, ONLY_VLLM, Live, live_server, make_config, needs_vllm

from linch_rust import ContextBuildEvent, ResultEvent, callback

pytestmark = [pytest.mark.live, needs_vllm]

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]


def block(tag: str, tokens: int = 100) -> dict[str, str]:
    # four characters per token: `tokens` * 4 characters in all
    text = f"{tag} " + "x" * (tokens * 4 - len(tag) - 1)
    return {"text": text}


def make_callback(policy: str, tags: list[str]) -> Any:
    @callback("context_build", policy, 1)
    async def build(_request: dict[str, Any]) -> dict[str, Any]:
        return {"blocks": [block(t) for t in tags]}

    return build


async def one_run(tee: Tee, spec: dict[str, Any], callbacks: list[Any]) -> tuple[Any, Any]:
    config = make_config(LIVE, LIVE.spec(**spec))
    config["provider"] = {"id": LIVE_KIND, "base_url": tee.url}
    async with live_server() as server:
        agent = await server.open_agent(config, callbacks=callbacks)
        run = await agent.run("b1", "Reply with the single word ok.")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        events = [e.event for e in await agent.events(run.id)]
    builds = [e for e in events if isinstance(e, ContextBuildEvent)]
    return builds[0], tee.chat()[0]


async def test_a_context_build_section_over_the_budget_drops_whole_blocks_in_order() -> None:
    """B1: 5 blocks of 100 tokens, cap 250: beta, alpha#1, alpha#0 go; gamma and delta stay."""
    tee = Tee("budget_drop")
    out: dict[str, Any] = {}
    try:
        callbacks = [
            make_callback("alpha", ["ALPHA-0", "ALPHA-1"]),
            make_callback("beta", ["BETA-0"]),
            make_callback("gamma", ["GAMMA-0"]),
            make_callback("delta", ["DELTA-0"]),
        ]
        budget = {"max_tokens": 250, "drop_order": ["gamma"], "pinned": ["delta"]}
        build, request = await one_run(tee, {"context_budget": budget}, callbacks)
        out["budget"] = build.budget
        last = msg_text(request["body"]["messages"][-1])
        out["sent"] = {t: t in last for t in ("ALPHA-0", "ALPHA-1", "BETA-0", "GAMMA-0", "DELTA-0")}
        out["prompt_tokens"] = request.get("prompt_tokens")

        for cap in (350, 150):
            teeN = Tee(f"budget_cap_{cap}")
            try:
                buildN, requestN = await one_run(
                    teeN,
                    {
                        "context_budget": {
                            "max_tokens": cap,
                            "drop_order": ["gamma"],
                            "pinned": ["delta"],
                        }
                    },
                    callbacks,
                )
            finally:
                teeN.close()
            out[f"cap_{cap}"] = {
                "dropped": sorted((d["source"], d["index"]) for d in buildN.budget["dropped"]),
                "used": buildN.budget["used_tokens"],
                "sent": [
                    t
                    for t in ("ALPHA-0", "ALPHA-1", "BETA-0", "GAMMA-0", "DELTA-0")
                    if t in msg_text(requestN["body"]["messages"][-1])
                ],
            }
        tee2 = Tee("budget_pinned_over")
        try:
            tight = {"max_tokens": 50, "drop_order": ["gamma"], "pinned": ["delta"]}
            build2, request2 = await one_run(tee2, {"context_budget": tight}, callbacks)
        finally:
            tee2.close()
        out["pinned_over"] = {
            "budget": build2.budget,
            "sent": {
                t: t in msg_text(request2["body"]["messages"][-1])
                for t in ("ALPHA-0", "ALPHA-1", "BETA-0", "GAMMA-0", "DELTA-0")
            },
        }
    finally:
        tee.close()
        dump("budget_drop", out)
    dropped = sorted((d["source"], d["index"]) for d in out["budget"]["dropped"])
    assert dropped == [("alpha", 0), ("alpha", 1), ("beta", 0)], out["budget"]
    # The order of dropping: the later unlisted callback first, then the last block of the earlier
    # one.
    assert out["cap_350"]["dropped"] == [("alpha", 1), ("beta", 0)], out["cap_350"]
    assert out["cap_350"]["sent"] == ["ALPHA-0", "GAMMA-0", "DELTA-0"]
    # Then the callbacks of `drop_order`; the pinned one never.
    assert out["cap_150"]["sent"] == ["DELTA-0"], out["cap_150"]
    assert out["budget"]["used_tokens"] <= 250 and out["budget"]["trimmed"] is True
    assert out["sent"] == {
        "ALPHA-0": False,
        "ALPHA-1": False,
        "BETA-0": False,
        "GAMMA-0": True,
        "DELTA-0": True,
    }
    pinned = out["pinned_over"]
    assert pinned["sent"]["DELTA-0"] and not pinned["sent"]["GAMMA-0"], pinned
    assert pinned["budget"].get("over_by", 0) > 0, pinned["budget"]


def lorem(tokens: int) -> str:
    return " ".join(["lorem ipsum"] * (tokens // 2))


async def test_system_sections_go_first_by_order_and_cost_their_tokens() -> None:
    """B3: sections sorted by `order`, then the prompt, then `append`; token deltas."""
    out: dict[str, Any] = {}
    sections = [
        {"name": "c", "text": "SECTION-C " + lorem(300), "order": 20},
        {"name": "a", "text": "SECTION-A goal", "order": 0},
        {"name": "b", "text": "SECTION-B " + lorem(300), "order": 10},
    ]
    runs = {
        "baseline": {},
        "sections": {"system": {"sections": sections, "append": "APPEND-Z end"}},
        "sections_doubled": {
            "system": {
                "sections": [dict(s, text=s["text"] + " " + lorem(300)) for s in sections],
                "append": "APPEND-Z end",
            }
        },
    }
    for label, spec in runs.items():
        tee = Tee(f"sections_{label}")
        try:
            _, request = await one_run(tee, dict(spec), [])
        finally:
            tee.close()
        system = msg_text(request["body"]["messages"][0])
        out[label] = {
            "system_role": request["body"]["messages"][0]["role"],
            "prompt_tokens": request.get("prompt_tokens"),
            "system_chars": len(system),
            "positions": {
                k: system.find(k)
                for k in ("SECTION-A", "SECTION-B", "SECTION-C", "terse test agent", "APPEND-Z")
            },
        }
    dump("sections_order_tokens", out)
    pos = out["sections"]["positions"]
    assert 0 <= pos["SECTION-A"] < pos["SECTION-B"] < pos["SECTION-C"] < pos["terse test agent"], (
        pos
    )
    assert pos["terse test agent"] < pos["APPEND-Z"]
    base = out["baseline"]["prompt_tokens"]
    with_sections = out["sections"]["prompt_tokens"]
    doubled = out["sections_doubled"]["prompt_tokens"]
    print(f"prompt tokens: baseline {base}, sections {with_sections}, doubled {doubled}")
    assert with_sections > base + 400
    assert doubled > with_sections + 400
