from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_live_prompt_cache.py"
    spec = importlib.util.spec_from_file_location("live_prompt_cache_benchmark_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_prompt_cache_result_renders_json_and_markdown():
    mod = _load_script()
    result = mod.LiveCacheResult(
        provider="openai-chat",
        model="deepseek-v4-flash",
        calls=[
            mod.LiveCacheCall(
                call=1,
                elapsed_ms=100.0,
                input_tokens=100,
                output_tokens=5,
                cache_read_tokens=0,
            ),
            mod.LiveCacheCall(
                call=2,
                elapsed_ms=80.0,
                input_tokens=100,
                output_tokens=5,
                cache_read_tokens=90,
                text="OK",
            ),
        ],
    )

    rendered = json.loads(
        mod.render_json(
            [result],
            prefix_lines=10,
            prefix_salt="",
            shared_prefix=False,
            mode="direct",
            scenario="baseline",
        )
    )
    assert rendered["kind"] == "linch_live_prompt_cache_benchmark"
    assert rendered["prefix_salt_set"] is False
    assert rendered["shared_prefix"] is False
    assert rendered["mode"] == "direct"
    assert rendered["scenario"] == "baseline"
    assert rendered["results"][0]["mode"] == "direct"
    assert rendered["results"][0]["scenario"] == "baseline"
    assert rendered["results"][0]["totals"]["cache_read_tokens"] == 90
    assert rendered["results"][0]["estimated_prompt_tokens"] == 200
    assert rendered["results"][0]["cache_read_ratio"] == 0.45
    assert rendered["results"][0]["warm_cache_read_ratio"] == 0.9

    markdown = mod.render_markdown(
        [result],
        prefix_lines=10,
        prefix_salt="fresh",
        shared_prefix=True,
        mode="tool-loop",
        scenario="all",
    )
    assert "Linch Live Prompt Cache Benchmark" in markdown
    assert "- prefix_salt_set: True" in markdown
    assert "- shared_prefix: True" in markdown
    assert "- mode: tool-loop" in markdown
    assert "- scenario: all" in markdown
    assert (
        "| openai-chat | baseline | direct | 2 | 0 | 0 | 200 | 200 | 90 | 0 | 45.00% | 90.00% |"
    ) in markdown


def test_build_static_prefix_is_deterministic():
    mod = _load_script()

    assert mod.build_static_prefix(3) == mod.build_static_prefix(3)
    assert mod.build_static_prefix(3, salt="a") != mod.build_static_prefix(3, salt="b")
    assert "cache-line-0002" in mod.build_static_prefix(3)
