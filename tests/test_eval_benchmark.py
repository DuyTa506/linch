from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_benchmark.py"
    spec = importlib.util.spec_from_file_location("eval_benchmark_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_eval_benchmark_compares_scripted_targets(tmp_path):
    from linch import Agent
    from linch.evals import (
        EvalBenchmarkTarget,
        ScriptedProvider,
        TextTurn,
        load_eval_suite,
        run_eval_benchmark,
    )
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    suite_path = tmp_path / "suite.json"
    _write_json(
        suite_path,
        {
            "name": "capitals",
            "cases": [
                {"prompt": "Capital of France?", "expected": "Paris"},
                {"prompt": "Capital of Germany?", "expected": "Berlin"},
            ],
            "scorers": [{"type": "text_contains", "substring": "{expected}"}],
        },
    )
    suite = load_eval_suite(suite_path)

    passing = Agent(
        model="scripted",
        provider=ScriptedProvider([TextTurn("Paris"), TextTurn("Berlin")]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    failing = Agent(
        model="scripted",
        provider=ScriptedProvider([TextTurn("Paris"), TextTurn("Paris")]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    result = await run_eval_benchmark(
        suite,
        [
            EvalBenchmarkTarget("passing", passing),
            EvalBenchmarkTarget("failing", failing),
        ],
    )

    assert result.suite_name == "capitals"
    assert result.total_targets == 2
    assert result.passed_targets == 1
    assert result.targets[0].result.pass_rate == 1.0
    assert result.targets[1].result.pass_rate == 0.5
    assert result.targets[0].duration_ms >= 0
    assert result.to_dict()["targets"][0]["name"] == "passing"
    assert "Linch Eval Benchmark: capitals" in result.to_markdown()


@pytest.mark.asyncio
async def test_eval_benchmark_script_loads_files_and_renders_json(tmp_path, capsys):
    suite_path = tmp_path / "suite.json"
    turns_path = tmp_path / "turns.json"
    _write_json(
        suite_path,
        {
            "name": "smoke",
            "cases": [{"prompt": "Say ok", "expected": "ok"}],
            "scorers": [{"type": "text_contains"}],
        },
    )
    _write_json(turns_path, {"turns": [{"type": "text", "text": "ok"}]})

    code = await _load_script()._main(
        [
            str(suite_path),
            "--scripted",
            f"candidate={turns_path}",
            "--format",
            "json",
        ]
    )

    assert code == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["suite"] == "smoke"
    assert rendered["targets"][0]["name"] == "candidate"
    assert rendered["targets"][0]["pass_rate"] == 1.0


@pytest.mark.asyncio
async def test_eval_benchmark_script_fails_under_threshold(tmp_path):
    suite_path = tmp_path / "suite.json"
    turns_path = tmp_path / "turns.json"
    _write_json(
        suite_path,
        {
            "cases": [{"prompt": "Say ok", "expected": "ok"}],
            "scorers": [{"type": "text_contains"}],
        },
    )
    _write_json(turns_path, [{"type": "text", "text": "nope"}])

    code = await _load_script()._main(
        [
            str(suite_path),
            "--scripted",
            str(turns_path),
            "--fail-under",
            "1.0",
        ]
    )

    assert code == 1
