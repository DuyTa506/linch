from __future__ import annotations

import threading
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_agent_discovery_runs_off_the_event_loop(tmp_path, monkeypatch: Any) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "helper.md").write_text(
        "---\ndescription: A helper agent.\n---\nBody\n",
        encoding="utf-8",
    )

    import linch.subagents.loader as loader_mod

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real = loader_mod._load_agents_from_dir_sync

    def spy(config_dir: str) -> Any:
        seen["thread"] = threading.get_ident()
        return real(config_dir)

    monkeypatch.setattr(loader_mod, "_load_agents_from_dir_sync", spy)

    result = await loader_mod.load_agents_from_dir(str(tmp_path))

    assert [a.name for a in result.agents] == ["helper"]
    assert seen["thread"] != loop_thread
