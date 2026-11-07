"""Task tools share one task list across an agent tree."""

from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


def _ctx(session_id: str, store: Any) -> Any:
    from linch.tools.base import ToolContext

    return ToolContext(cwd=".", session_id=session_id, run_id="r", session_store=store)


def _tool_result_text(request: Any) -> str:
    return "".join(
        str(block.content)
        for block in request.messages[-1].content
        if getattr(block, "type", None) == "tool_result"
    )


async def test_worker_lists_and_updates_the_coordinator_tasks() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.registry import empty_tools
    from linch.tools.tasks import TaskCreateTool, TaskListTool, TaskUpdateTool

    provider = ScriptedProvider(
        [
            ToolUseTurn("TaskList", {}, tool_id="list_1"),
            ToolUseTurn("TaskUpdate", {"id": "1", "status": "completed"}, tool_id="update_1"),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider,
        model="m",
        tools=empty_tools(TaskCreateTool(), TaskListTool(), TaskUpdateTool()),
    )
    try:
        parent = await agent.session()
        await TaskCreateTool().execute(
            {"subject": "Ship it", "description": "Release the build"},
            _ctx(parent.id, parent.store),
        )

        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="work the task list",
                display_name="w",
                subagent_run_id="sa_tasks",
            )
        )

        assert not result.errored
        assert "#1 [pending] Ship it" in _tool_result_text(provider.requests[1])
        assert "updated: [completed] Ship it" in _tool_result_text(provider.requests[2])
        task = await parent.store.get_task(parent.id, "1")
        assert task is not None and task.status == "completed"
    finally:
        await agent.close()


async def test_unrelated_sessions_keep_separate_task_lists() -> None:
    from linch.sessions import InMemorySessionStore
    from linch.tools.tasks import TaskCreateTool, TaskGetTool, TaskListTool

    store = InMemorySessionStore()
    root = await store.create()
    child = await store.create(meta={"parentSessionId": root.id})
    grandchild = await store.create(meta={"parentSessionId": child.id})
    other = await store.create()

    await TaskCreateTool().execute(
        {"subject": "Root task", "description": "d"}, _ctx(root.id, store)
    )

    nested = await TaskGetTool().execute({"id": "1"}, _ctx(grandchild.id, store))
    assert not nested.is_error and "Root task" in nested.content
    unrelated = await TaskListTool().execute({}, _ctx(other.id, store))
    assert unrelated.content == "No tasks found."
