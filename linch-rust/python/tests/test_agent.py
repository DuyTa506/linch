"""The agent handle: principals for role rules, closing it, and skipped definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Agent,
    AgentNotFound,
    Allow,
    Principal,
    ResultEvent,
    Server,
    Skipped,
    SuspendedEvent,
    tool,
)
from linch_rust._rpc import Connection

ADMIN: Principal = {"tenant": "t", "user": "ada", "roles": ["admin"]}


def admins_save(turns: list[dict[str, Any]]) -> Any:
    """A config in mode `default` whose rule lets an `admin` call `save` unasked."""
    rule = {"type": "tool", "tool": "save", "decision": "allow", "role": "admin"}
    layers = [{"name": "app", "rules": [rule]}]
    return config(turns, permissions={"mode": "default", "project_root": "/", "layers": layers})


@tool(scope="write")
async def save(text: str) -> str:
    return f"saved {text}"


async def test_a_role_rule_matches_the_principal_a_run_starts_with() -> None:
    # Each call to the model takes the next turn: s1 asks, then s2 runs.
    turns = [
        tool_turn("c1", "save", {"text": "a"}),
        tool_turn("c2", "save", {"text": "a"}),
        text_turn("Done."),
    ]
    async with Server() as server:
        agent = await server.open_agent(admins_save(turns), tools=[save])
        asked = await (await agent.run("s1", "Save a")).result()
        allowed = await (await agent.run("s2", "Save a", principal=ADMIN)).result()

    assert isinstance(asked, SuspendedEvent) and asked.pending_request_ids == ["c1"]
    assert isinstance(allowed, ResultEvent) and allowed.final_text == "Done."


async def test_an_approve_continues_the_run_as_its_principal() -> None:
    turns = [tool_turn("c1", "save", {"text": "a"}), tool_turn("c2", "save", {"text": "b"})]
    async with Server() as server:
        agent = await server.open_agent(admins_save([*turns, text_turn("Done.")]), tools=[save])
        run = await agent.run("s1", "Save a and b")
        assert isinstance(await run.result(), SuspendedEvent)
        approved = await agent.approve(run.id, {"c1": Allow()}, principal=ADMIN)
        result = await approved.result()

    assert isinstance(result, ResultEvent) and result.final_text == "Done."


async def test_run_resume_and_approve_send_the_principal() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class Recording:
        async def request_run(self, method: str, params: dict[str, Any]) -> Any:
            sent.append((method, params))
            return "r1", None

    agent = Agent(cast(Connection, Recording()), "agent-1", "c")
    await agent.run("s1", "hi", principal=ADMIN)
    await agent.resume("r1", principal=ADMIN)
    await agent.approve("r1", {}, principal=ADMIN)
    await agent.resume("r1")

    methods = [method for method, _ in sent]
    assert methods == ["run.start", "run.resume", "run.approve", "run.resume"]
    assert all(params["principal"] == ADMIN for _, params in sent[:3])
    assert "principal" not in sent[3][1]


async def test_a_closed_agent_is_gone_from_the_server() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("unused")]))
        await agent.close()
        with pytest.raises(AgentNotFound):
            await agent.run("s1", "hi")
        with pytest.raises(AgentNotFound):
            await agent.close()


async def test_an_agent_lists_the_definitions_it_skipped(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / ".linch" / "skills" / "empty").mkdir(parents=True)
    async with Server() as server:
        skipping = await server.open_agent(config([], workspace=str(root), skills=True))
        clean = await server.open_agent(config([]))

    [skipped] = skipping.skipped
    assert isinstance(skipped, Skipped)
    assert skipped.path == str(root / ".linch" / "skills" / "empty")
    assert skipped.reason == "missing_skill_md" and skipped.detail
    assert clean.skipped == []
