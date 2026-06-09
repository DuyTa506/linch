from __future__ import annotations

import pytest


def test_build_run_report_summarizes_events():
    from linch import build_run_report
    from linch.events import (
        ContextBuildEvent,
        LoopGuardEvent,
        PermissionRequestEvent,
        PermissionRequestItem,
        ResultEvent,
        SystemEvent,
        ToolCallEndEvent,
        ToolCallStartEvent,
        UsageEvent,
    )
    from linch.tools import ToolResult
    from linch.types import Usage

    events = [
        SystemEvent(
            session_id="s1",
            run_id="r1",
            model="fake",
            tools=["Search"],
            permission_mode="default",
            cwd=".",
        ),
        ContextBuildEvent(
            system_blocks=1,
            messages=3,
            selected_tools=["Search"],
            budget={"max_tokens": 800},
            metadata={"source": "docs"},
        ),
        PermissionRequestEvent(
            requests=[
                PermissionRequestItem(
                    tool_use_id="t1",
                    tool_name="Search",
                    input={"query": "x"},
                    summary="Search x",
                )
            ]
        ),
        ToolCallStartEvent(
            tool_use_id="t1",
            tool_name="Search",
            input={"query": "x"},
            summary="Search x",
        ),
        ToolCallEndEvent(
            tool_use_id="t1",
            tool_name="Search",
            result="ok",
            is_error=False,
            duration_ms=12,
            tool_result=ToolResult(content="ok", metadata={"rows": 1}),
        ),
        LoopGuardEvent(reason="max_turns", detail="stopped", action="stop"),
        UsageEvent(
            usage=Usage(input_tokens=10, output_tokens=5),
            cumulative=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.01,
            cumulative_cost_usd=0.01,
        ),
        ResultEvent(
            subtype="success",
            stop_reason="end_turn",
            total_usage=Usage(input_tokens=10, output_tokens=5),
            duration_ms=100,
            final_text="done",
            total_cost_usd=0.01,
        ),
    ]

    report = build_run_report(events)

    assert report.run_id == "r1"
    assert report.session_id == "s1"
    assert report.status == "completed"
    assert report.event_count == len(events)
    assert report.tool_calls[0]["tool_name"] == "Search"
    assert report.permission_requests[0]["summary"] == "Search x"
    assert report.context_builds[0]["metadata"] == {"source": "docs"}
    assert report.loop_guards[0]["reason"] == "max_turns"
    assert report.usage["cumulative_cost_usd"] == 0.01
    assert report.final["final_text"] == "done"
    assert report.long_run["context"]["builds"] == 1
    assert report.long_run["context"]["metadata_keys"] == ["source"]
    assert report.long_run["quality"]["completed"] is True
    assert "Tool Calls" in report.to_markdown()


def test_build_run_report_includes_long_run_memory_and_recovery_signals():
    from linch import build_run_report
    from linch.events import (
        ContextBuildEvent,
        ResultEvent,
        ToolCallEndEvent,
        ToolCallStartEvent,
    )
    from linch.tools import Citation, ToolResult
    from linch.types import Usage

    events = [
        ContextBuildEvent(
            system_blocks=2,
            messages=8,
            selected_tools=["SearchMemory", "Read"],
            budget={"max_tokens": 1000, "used_tokens": 900, "remaining_tokens": 100},
            metadata={"rag": "policy"},
        ),
        ContextBuildEvent(
            system_blocks=1,
            messages=5,
            selected_tools=["SearchMemory"],
            budget={"max_tokens": 500, "used_tokens": 520, "trimmed": True},
            metadata={"memory_namespace": "tenant-a"},
        ),
        ToolCallStartEvent(
            tool_use_id="m1",
            tool_name="SearchMemory",
            input={"query": "pto"},
            summary="SearchMemory(pto)",
        ),
        ToolCallEndEvent(
            tool_use_id="m1",
            tool_name="SearchMemory",
            is_error=False,
            tool_result=ToolResult(
                content="[policy-1] PTO rolls over",
                metadata={
                    "namespace": "tenant-a",
                    "result_ids": ["policy-1"],
                    "tier_counts": {"semantic": 1},
                },
                citations=[
                    Citation(
                        id="policy-1",
                        source="memory:tenant-a",
                        metadata={"tier": "semantic"},
                    )
                ],
            ),
        ),
        ToolCallEndEvent(
            tool_use_id="r1",
            tool_name="Read",
            is_error=True,
            tool_result=ToolResult(
                content="missing",
                is_error=True,
                recovery_hint="Try the generated offload reference.",
            ),
        ),
        ResultEvent(
            subtype="success",
            stop_reason="end_turn",
            total_usage=Usage(input_tokens=10, output_tokens=5),
            duration_ms=100,
            final_text="done",
            total_cost_usd=0.02,
        ),
    ]

    report = build_run_report(events)

    assert report.long_run["context"]["trimmed_builds"] == 1
    assert report.long_run["context"]["max_used_tokens"] == 900
    assert report.long_run["context"]["selected_tool_counts"] == {
        "Read": 1,
        "SearchMemory": 2,
    }
    assert report.long_run["memory"]["searches"] == 1
    assert report.long_run["memory"]["result_ids"] == ["policy-1"]
    assert report.long_run["memory"]["namespaces"] == ["tenant-a"]
    assert report.long_run["memory"]["tier_counts"] == {"semantic": 1}
    assert report.long_run["quality"]["failed_tool_calls"] == 1
    assert report.long_run["quality"]["recovery_hints"] == 1
    assert "Long-Run Signals" in report.to_markdown()


@pytest.mark.asyncio
async def test_load_run_report_from_run_store():
    from linch import InMemoryRunStore, load_run_report
    from linch.events import ResultEvent, SystemEvent
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    store = InMemoryRunStore()
    run = await store.create_run("s1", id="r1")
    checkpoint = RunCheckpoint(
        phase="completed",
        prompt="hello",
        turn_index=1,
        total_usage=Usage(input_tokens=1, output_tokens=1),
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
            total_usage=Usage(input_tokens=1, output_tokens=1),
            duration_ms=5,
            final_text="ok",
        ),
    )

    report = await load_run_report(store, "r1")

    assert report.run_id == "r1"
    assert report.status == "completed"
    assert report.phase == "completed"
    assert report.checkpoint["prompt"] == "hello"
    assert len(report.timeline) == 2
