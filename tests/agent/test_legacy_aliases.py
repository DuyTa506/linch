from __future__ import annotations

import pytest

from linch.errors import ConfigError
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def test_agent_does_not_read_camel_case_permission_callback() -> None:
    async def allow(_request: object) -> dict[str, str]:
        return {"behavior": "allow"}

    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        permissions={"mode": "default", "canUseTool": allow},
        run_store=None,
    )

    assert agent.permission_engine.can_use_tool is None


@pytest.mark.parametrize(
    "stored",
    [
        {"name": "legacy", "substitutedBody": "body", "invokedAt": 1.0},
        {"name": "missing-body", "invoked_at": 1.0},
        {"name": "missing-time", "substituted_body": "body"},
        {"name": "bad-time", "substituted_body": "body", "invoked_at": "later"},
        "not-a-record",
    ],
)
async def test_agent_rejects_malformed_invoked_skill_records(stored: object) -> None:
    from linch.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    await store.create(id="session")
    await store.set_invoked_skills("session", [stored])  # type: ignore[list-item]
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=store,
        run_store=None,
    )

    try:
        with pytest.raises(ConfigError, match="invoked_skills"):
            await agent.session(id="session")
    finally:
        await agent.close()


async def test_agent_restores_canonical_invoked_skill_records() -> None:
    from linch.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    await store.create(id="session")
    await store.set_invoked_skills(
        "session",
        [{"name": "verify", "substituted_body": "body", "invoked_at": 3.5}],
    )
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=store,
        run_store=None,
    )

    try:
        session = await agent.session(id="session")
        assert [
            (row.name, row.substituted_body, row.invoked_at) for row in session.invoked_skills
        ] == [("verify", "body", 3.5)]
    finally:
        await agent.close()
