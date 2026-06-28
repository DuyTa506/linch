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
    assert report.summary["duration_ms"] == 100
    assert report.summary["event_counts"] == {
        "context_build": 1,
        "loop_guard": 1,
        "permission_request": 1,
        "result": 1,
        "system": 1,
        "tool_call_end": 1,
        "tool_call_start": 1,
        "usage": 1,
    }
    assert report.summary["usage"]["total_tokens"] == 15
    assert report.summary["usage"]["total_cost_usd"] == 0.01
    assert report.summary["tools"]["average_duration_ms"] == 12
    assert report.summary["tools"]["slowest_tool"]["tool_name"] == "Search"
    assert report.summary["tools"]["top_slowest"][0]["tool_name"] == "Search"
    assert report.summary["tools"]["by_name_errors"] == {}
    assert report.summary["context"]["pressure"] == "none"
    assert report.long_run["context"]["builds"] == 1
    assert report.long_run["context"]["metadata_keys"] == ["source"]
    assert report.long_run["quality"]["completed"] is True
    assert "Summary" in report.to_markdown()
    assert "Tool Calls" in report.to_markdown()


def test_float_tool_duration_is_counted_not_dropped():
    # Regression: a float duration_ms (e.g. from a custom provider/tool) must be
    # coerced into the summary aggregates, not silently dropped to 0.
    from linch import build_run_report
    from linch.events import ToolCallEndEvent, ToolCallStartEvent
    from linch.tools import ToolResult

    events = [
        ToolCallStartEvent(tool_use_id="t1", tool_name="Slow", input={}, summary="Slow"),
        ToolCallEndEvent(
            tool_use_id="t1",
            tool_name="Slow",
            result="ok",
            is_error=False,
            duration_ms=42.7,
            tool_result=ToolResult(content="ok"),
        ),
    ]

    report = build_run_report(events)

    assert report.summary["tools"]["total_duration_ms"] == 42
    assert report.summary["tools"]["slowest_tool"]["tool_name"] == "Slow"


def test_run_report_ranks_top_slowest_tools_deterministically():
    from linch import build_run_report
    from linch.events import ToolCallEndEvent, ToolCallStartEvent

    calls = [
        ("fast", "Read", 10),
        ("slow-a", "Search", 50),
        ("slow-b", "Write", 50),
        ("medium", "List", 25),
        ("fifth", "Glob", 5),
        ("sixth", "Patch", 1),
    ]
    events = []
    for tool_use_id, tool_name, duration_ms in calls:
        events.append(
            ToolCallStartEvent(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                input={},
                summary=tool_use_id,
            )
        )
        events.append(
            ToolCallEndEvent(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                is_error=False,
                duration_ms=duration_ms,
            )
        )

    report = build_run_report(events)

    assert [call["tool_use_id"] for call in report.summary["tools"]["top_slowest"]] == [
        "slow-a",
        "slow-b",
        "medium",
        "fast",
        "fifth",
    ]


def test_run_report_summarizes_failed_tools_by_name_and_event_order():
    from linch import build_run_report
    from linch.events import ToolCallEndEvent, ToolCallStartEvent
    from linch.tools import ToolResult

    events = [
        ToolCallStartEvent(tool_use_id="r1", tool_name="Read", input={}, summary="read one"),
        ToolCallEndEvent(
            tool_use_id="r1",
            tool_name="Read",
            result="missing file",
            is_error=True,
            duration_ms=9,
        ),
        ToolCallStartEvent(tool_use_id="s1", tool_name="Search", input={}, summary="search"),
        ToolCallEndEvent(
            tool_use_id="s1",
            tool_name="Search",
            is_error=False,
            duration_ms=3,
        ),
        ToolCallStartEvent(tool_use_id="r2", tool_name="Read", input={}, summary="read two"),
        ToolCallEndEvent(
            tool_use_id="r2",
            tool_name="Read",
            is_error=True,
            duration_ms=12,
            tool_result=ToolResult(content="permission denied\ntry another path", is_error=True),
        ),
    ]

    report = build_run_report(events)

    assert report.summary["tools"]["by_name"] == {"Read": 2, "Search": 1}
    assert report.summary["tools"]["by_name_errors"] == {"Read": 2}
    assert report.summary["tools"]["top_failures"] == [
        {
            "tool_use_id": "r1",
            "tool_name": "Read",
            "summary": "read one",
            "duration_ms": 9,
            "result": "missing file",
            "error": "missing file",
        },
        {
            "tool_use_id": "r2",
            "tool_name": "Read",
            "summary": "read two",
            "duration_ms": 12,
            "result": "permission denied try another path",
            "error": "permission denied try another path",
        },
    ]
    markdown = report.to_markdown()
    assert "Top Slow Tools" in markdown
    assert "Failing Tools" in markdown


@pytest.mark.parametrize(
    ("budget", "pressure"),
    [
        ({"max_tokens": 100, "used_tokens": 74}, "none"),
        ({"max_tokens": 100, "used_tokens": 75}, "moderate"),
        ({"max_tokens": 100, "used_tokens": 90}, "high"),
        ({"max_tokens": 100, "used_tokens": 100}, "high"),
        ({"max_tokens": 100, "used_tokens": 101}, "over"),
        ({"used_tokens": 100}, "none"),
    ],
)
def test_context_utilization_maps_to_pressure_labels(budget, pressure):
    from linch import build_run_report
    from linch.events import ContextBuildEvent

    report = build_run_report(
        [
            ContextBuildEvent(
                system_blocks=1,
                messages=1,
                selected_tools=[],
                budget=budget,
                metadata={},
            )
        ]
    )

    assert report.summary["context"]["pressure"] == pressure


def test_non_finite_budget_value_does_not_crash_report_building():
    # Regression: report building is a non-throwing read model. A NaN/Infinity
    # numeric field (json.loads accepts these, so they survive a persisted-event
    # round-trip) must be dropped, not int()-coerced into a ValueError/OverflowError.
    from linch import build_run_report
    from linch.events import ContextBuildEvent, ResultEvent
    from linch.types import Usage

    events = [
        ContextBuildEvent(
            system_blocks=1,
            messages=1,
            selected_tools=[],
            budget={"max_tokens": float("inf"), "used_tokens": float("nan")},
            metadata={},
        ),
        ResultEvent(
            subtype="success",
            stop_reason="end_turn",
            total_usage=Usage(input_tokens=1, output_tokens=1),
            duration_ms=float("nan"),
            final_text="done",
        ),
    ]

    report = build_run_report(events)  # must not raise

    # Non-finite budget value is dropped, not coerced.
    assert report.summary["context"]["max_used_tokens"] in (0, None)
    assert "Summary" in report.to_markdown()


def test_report_diagnostics_ignore_missing_and_non_finite_values():
    from linch import build_run_report
    from linch.events import ToolCallEndEvent

    events = [
        ToolCallEndEvent(
            tool_use_id="nan",
            tool_name="BadDuration",
            is_error=True,
            duration_ms=float("nan"),
        ),
        ToolCallEndEvent(
            tool_use_id="default",
            tool_name="DefaultDuration",
            is_error=True,
        ),
    ]

    report = build_run_report(events)

    assert report.summary["tools"]["top_slowest"] == [
        {
            "tool_use_id": "default",
            "tool_name": "DefaultDuration",
            "summary": "",
            "duration_ms": 0,
            "is_error": True,
        }
    ]
    assert report.summary["tools"]["top_failures"] == [
        {
            "tool_use_id": "nan",
            "tool_name": "BadDuration",
            "summary": "",
            "duration_ms": None,
        },
        {
            "tool_use_id": "default",
            "tool_name": "DefaultDuration",
            "summary": "",
            "duration_ms": 0,
        },
    ]


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
    assert report.summary["tools"]["total"] == 2
    assert report.summary["tools"]["failed"] == 1
    assert report.summary["tools"]["error_rate"] == 0.5
    assert report.summary["context"]["max_utilization"] == 1.04
    assert report.summary["risk"]["recovery_hints"] == 1
    assert "Long-Run Signals" in report.to_markdown()


def test_build_run_report_exposes_recovery_and_efficiency_counters():
    from linch import build_run_report
    from linch.events import (
        CompactionEvent,
        HookEventRecord,
        ModelFallbackEvent,
        ResultEvent,
        ToolCallEndEvent,
        VerificationEvent,
    )
    from linch.tools import ToolResult
    from linch.types import Usage

    events = [
        CompactionEvent(
            messages_before=20,
            messages_after=8,
            tokens_before=10_000,
            tokens_after=4_000,
            strategy="micro",
        ),
        ModelFallbackEvent(
            from_model="primary",
            to_model="backup",
            reason="overloaded",
        ),
        VerificationEvent(
            verifier="judge",
            action="retry",
            feedback="fix it",
            attempt=1,
        ),
        HookEventRecord(
            event="before_final_answer",
            hook="policy",
            action="retry",
            reason="needs citation",
        ),
        ToolCallEndEvent(
            tool_use_id="t1",
            tool_name="Search",
            is_error=False,
            tool_result=ToolResult(
                content="preview",
                truncated=True,
                metadata={"offloaded_to": "/offload/Search_t1.txt"},
            ),
        ),
        ToolCallEndEvent(
            tool_use_id="t2",
            tool_name="Read",
            is_error=False,
            tool_result=ToolResult(content="small"),
        ),
        ResultEvent(
            subtype="success",
            stop_reason="end_turn",
            total_usage=Usage(
                input_tokens=30,
                output_tokens=10,
                cache_read_tokens=15,
                cache_creation_tokens=5,
            ),
            duration_ms=100,
            final_text="done",
        ),
    ]

    report = build_run_report(events)

    assert report.summary["usage"]["total_tokens"] == 60
    assert report.summary["usage"]["cache_read_ratio"] == 0.3
    assert report.summary["recovery"] == {
        "compactions": 1,
        "compaction_tokens_saved": 6000,
        "model_fallbacks": 1,
        "fallback_paths": [
            {
                "from_model": "primary",
                "to_model": "backup",
                "reason": "overloaded",
            }
        ],
        "verification_retries": 1,
        "hook_retries": 1,
        "result_offloads": 1,
        "offload_hit_rate": 0.5,
    }
    assert report.summary["risk"]["model_fallbacks"] == 1
    assert report.summary["risk"]["verification_retries"] == 1
    assert report.summary["risk"]["hook_retries"] == 1
    assert "cache read ratio: 0.3" in report.to_markdown()


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
