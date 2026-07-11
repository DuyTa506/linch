from __future__ import annotations

import threading
from typing import Any

import pytest


class _FakeProvider:
    id = "fake"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):  # pragma: no cover - these tests do not call the model
        raise AssertionError("provider should not be called")


def _make_agent(config_dir: str):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import default_tools

    return Agent(
        model="model-x",
        provider=_FakeProvider(),
        tools=default_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        config_dir=config_dir,
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        result_offload=None,
    )


@pytest.mark.asyncio
async def test_skill_discovery_runs_off_the_event_loop(tmp_path, monkeypatch: Any) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: A demo skill.\n---\n# Demo\n",
        encoding="utf-8",
    )

    import linch.skills.loader as loader_mod

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real = loader_mod.load_skills_from_dir

    def spy(config_dir: str, builtin_tool_names: set[str]):
        seen["thread"] = threading.get_ident()
        return real(config_dir, builtin_tool_names)

    monkeypatch.setattr(loader_mod, "load_skills_from_dir", spy)

    agent = _make_agent(str(tmp_path))
    await agent.session()

    assert "demo" in agent.skills
    assert seen["thread"] != loop_thread
