from __future__ import annotations

import importlib.util
from pathlib import Path

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
async def test_run_report_script_renders_from_sqlite_store(tmp_path):
    from linch.events import ResultEvent, SystemEvent
    from linch.run_store import RunCheckpoint, SqliteRunStore
    from linch.types import Usage

    db = tmp_path / "runs.db"
    async with SqliteRunStore(db) as store:
        run = await store.create_run("s1", id="r1")
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
                run_id="r1",
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

    text = await _load_script().render_report(db, "r1", output="summary")

    assert "Linch run report: r1" in text
    assert "status: completed" in text
    assert "- total_tokens: 3" in text
