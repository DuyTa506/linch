from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_report.py"
    spec = importlib.util.spec_from_file_location("run_report_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_report_summary_format_is_scannable():
    from linch import build_run_report
    from linch.events import ResultEvent, ToolCallEndEvent
    from linch.types import Usage

    report = build_run_report(
        [
            ToolCallEndEvent(
                tool_use_id="t1",
                tool_name="Search",
                is_error=True,
                duration_ms=25,
            ),
            ResultEvent(
                subtype="success",
                stop_reason="end_turn",
                total_usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3),
                duration_ms=120,
                final_text="done",
                total_cost_usd=0.01,
            ),
        ]
    )

    text = _load_script().format_summary(report)

    assert "Linch run report:" in text
    assert "duration_ms: 120" in text
    assert "- total_tokens: 18" in text
    assert "- cache_read_ratio: 0.2308" in text
    assert "- failed: 1" in text
    assert "- slowest: Search (25ms, error=True)" in text
    assert "- top slow:" in text
    assert "  - Search (25ms, error=True)" in text
    assert "- top failures:" in text
    assert "  - Search (25ms)" in text
    assert "- pressure: none" in text
    assert "Recovery" in text
    assert "- result_offloads: 0" in text
    assert "Risk" in text


@pytest.mark.asyncio
async def test_run_report_script_renders_from_postgres_store(pg_dsn):
    from linch import PostgresRunStore, RunCheckpoint
    from linch.events import ResultEvent, SystemEvent
    from linch.types import Usage

    run_id = uuid4().hex
    async with PostgresRunStore(pg_dsn) as store:
        run = await store.create_run("s1", id=run_id)
        checkpoint = RunCheckpoint(
            phase="completed",
            prompt="hello",
            turn_index=1,
            total_usage=Usage(input_tokens=1, output_tokens=2),
        )
        await store.save_checkpoint(run.id, checkpoint, status="completed")
        await store.append_event(
            run.id,
            SystemEvent(
                session_id="s1",
                run_id=run_id,
                model="fake",
                tools=[],
                permission_mode="skip-dangerous",
                cwd=".",
            ),
        )
        await store.append_event(
            run.id,
            ResultEvent(
                subtype="success",
                stop_reason="end_turn",
                total_usage=Usage(input_tokens=1, output_tokens=2),
                duration_ms=5,
                final_text="ok",
            ),
        )

    text = await _load_script().render_report(pg_dsn, run_id, output="summary")

    assert f"Linch run report: {run_id}" in text
    assert "status: completed" in text
    assert "- total_tokens: 3" in text


def test_report_parser_uses_environment_dsn_and_explicit_override(monkeypatch):
    monkeypatch.setenv("LINCH_POSTGRES_DSN", "postgresql://localhost/env")
    parser = _load_script().build_parser()
    assert parser.parse_args(["run"]).dsn == "postgresql://localhost/env"
    assert parser.parse_args(["run", "--dsn", "postgresql://localhost/cli"]).dsn == (
        "postgresql://localhost/cli"
    )
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["run", "--runs-db", "runs.db"])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_report_cli_requires_dsn(monkeypatch, capsys):
    monkeypatch.delenv("LINCH_POSTGRES_DSN", raising=False)
    with pytest.raises(SystemExit) as error:
        await _load_script()._main(["run"])
    assert error.value.code == 2
    assert "--dsn or set LINCH_POSTGRES_DSN" in capsys.readouterr().err
