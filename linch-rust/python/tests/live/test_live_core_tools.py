"""Live: host tools the model calls, parallel read tools, an error it recovers from."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from live_core_support import PROVIDERS, Live, ask, make_config, retry_once, text_of

from linch_rust import tool

pytestmark = pytest.mark.live


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_model_calls_a_host_tool_and_reads_its_result(live: Live) -> None:
    seen: list[dict[str, int]] = []

    @tool(scope="read")
    async def add_numbers(a: int, b: int) -> dict[str, int]:
        """Add two integers."""
        seen.append({"a": a, "b": b})
        return {"sum": a + b}

    trace = await ask(
        live,
        "Call the tool add_numbers with a=1234 and b=4321, then state the sum.",
        tools=(add_numbers,),
    )
    assert seen and seen[0] == {"a": 1234, "b": 4321}
    [end] = trace.ends("add_numbers")
    assert not end.is_error and json.loads(text_of(end)) == {"sum": 5555}
    assert "5555" in (trace.result.final_text or "")
    # The call is durable before it ran: start precedes end in the stream.
    kinds = [e.type for e in trace.events]
    assert kinds.index("tool_call_start") < kinds.index("tool_call_end")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_parallel_read_tools_run_at_the_same_time(live: Live) -> None:
    spans: dict[str, tuple[float, float]] = {}

    async def slow(name: str) -> str:
        start = time.monotonic()
        await asyncio.sleep(1.0)
        spans[name] = (start, time.monotonic())
        return f"{name}-done"

    @tool(scope="read")
    async def check_alpha() -> str:
        """Check the alpha service."""
        return await slow("alpha")

    @tool(scope="read")
    async def check_beta() -> str:
        """Check the beta service."""
        return await slow("beta")

    trace = await ask(
        live,
        "Check both services: call check_alpha and check_beta in the same response "
        "(two tool calls at once), then say both are done.",
        tools=(check_alpha, check_beta),
    )
    first = trace.assistants()[0].message["content"]
    assert [b["name"] for b in first if b["type"] == "tool_use"] == ["check_alpha", "check_beta"]
    assert set(spans) == {"alpha", "beta"}
    (a0, a1), (b0, b1) = spans["alpha"], spans["beta"]
    assert max(a0, b0) < min(a1, b1), f"read tools were serialized: {spans}"
    assert not any(e.is_error for e in trace.ends())


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_tool_error_is_read_by_the_model_which_recovers(live: Live) -> None:
    calls: list[str] = []

    @tool(scope="read")
    async def fetch(key: str) -> str:
        """Fetch a stored value by key."""
        calls.append(key)
        if key != "alpha":
            raise KeyError(f"unknown key {key!r}; the only valid key is 'alpha'")
        return "the value is 4242"

    trace = await ask(
        live,
        "Call fetch with key 'beta'. If it fails, read the error and call fetch again "
        "with the key it names. Then report the value.",
        tools=(fetch,),
        config=make_config(live, live.spec(max_turns=6)),
    )
    ends = trace.ends("fetch")
    assert calls[0] == "beta" and "alpha" in calls
    assert ends[0].is_error and "alpha" in text_of(ends[0])
    assert ends[0].error_code is None or isinstance(ends[0].error_code, str)
    assert not ends[-1].is_error and "4242" in text_of(ends[-1])
    assert trace.result.subtype == "success" and "4242" in (trace.result.final_text or "")
