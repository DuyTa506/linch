from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.modules import load_module


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_benchmark.py"
    return load_module(path, "eval_benchmark_script")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_eval_benchmark_compares_scripted_targets(tmp_path):
    from linch.ext.evals import (
        EvalBenchmarkTarget,
        load_eval_suite,
        run_eval_benchmark,
    )
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn

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

    passing = make_agent(
        ScriptedProvider([TextTurn("Paris"), TextTurn("Berlin")]), model="scripted"
    )
    failing = make_agent(ScriptedProvider([TextTurn("Paris"), TextTurn("Paris")]), model="scripted")

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
@pytest.mark.parametrize("failing", [False, True], ids=["success", "fail-under"])
async def test_eval_benchmark_script_loads_files_and_renders_json(tmp_path, capsys, failing):
    suite_path = tmp_path / "suite.json"
    turns_path = tmp_path / "turns.json"
    _write_json(
        suite_path,
        {
            **({"name": "smoke"} if not failing else {}),
            "cases": [{"prompt": "Say ok", "expected": "ok"}],
            "scorers": [{"type": "text_contains"}],
        },
    )
    _write_json(
        turns_path,
        {"turns": [{"type": "text", "text": "ok"}]}
        if not failing
        else [{"type": "text", "text": "nope"}],
    )
    args = (
        [str(suite_path), "--scripted", f"candidate={turns_path}", "--format", "json"]
        if not failing
        else [str(suite_path), "--scripted", str(turns_path), "--fail-under", "1.0"]
    )

    code = await _load_script()._main(args)
    assert code == (1 if failing else 0)
    if not failing:
        rendered = json.loads(capsys.readouterr().out)
        assert rendered["suite"] == "smoke"
        assert rendered["targets"][0]["name"] == "candidate"
        assert rendered["targets"][0]["pass_rate"] == 1.0
