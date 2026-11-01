from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.modules import load_module


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_prompt_cache.py"
    return load_module(path, "prompt_cache_benchmark_script")


@pytest.mark.asyncio
async def test_prompt_cache_probe_reports_disjoint_usage_buckets():
    mod = _load_script()
    provider = mod.PrefixCacheProbeProvider(tool_turns=0)
    request = mod.ProviderRequest(model="cache-probe", system=[], tools=[], messages=[])

    events = [event async for event in provider.stream(request)]
    usage = next(event["usage"] for event in events if event["type"] == "message_end")
    call = provider.calls[0]

    assert usage.input_tokens == max(
        0, call.prompt_tokens - usage.cache_read_tokens - usage.cache_creation_tokens
    )
    assert (
        usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
        == call.prompt_tokens
    )


@pytest.mark.asyncio
async def test_prompt_cache_benchmark_reports_cacheability_json(capsys):
    code = await _load_script()._main(["--tool-turns", "2", "--format", "json"])

    assert code == 0
    rendered = json.loads(capsys.readouterr().out)
    scenarios = {item["name"]: item for item in rendered["scenarios"]}

    assert rendered["kind"] == "linch_prompt_cache_benchmark"
    assert rendered["tool_turns"] == 2
    assert set(scenarios) == {
        "stable_tools",
        "dynamic_message_context",
        "dynamic_system_tail",
        "rotating_selected_tools",
    }
    assert scenarios["stable_tools"]["provider_calls"] == 3
    assert scenarios["stable_tools"]["cache_read_tokens"] > 0
    assert (
        scenarios["stable_tools"]["cache_read_ratio"]
        > scenarios["rotating_selected_tools"]["cache_read_ratio"]
    )
    assert len(scenarios["stable_tools"]["calls"]) == 3


@pytest.mark.asyncio
async def test_prompt_cache_benchmark_markdown_mentions_mock_scope(capsys):
    code = await _load_script()._main(["--tool-turns", "1"])

    assert code == 0
    text = capsys.readouterr().out
    assert "Linch Prompt Cache Benchmark" in text
    assert "Offline prefix-cache simulation using a mock provider" in text
    assert "stable_tools" in text
