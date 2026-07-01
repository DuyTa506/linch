from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_prompt_cache.py"
    spec = importlib.util.spec_from_file_location("prompt_cache_benchmark_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
