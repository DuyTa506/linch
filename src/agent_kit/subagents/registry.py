from __future__ import annotations

from .builtins import BUILT_IN_NAMED_AGENTS
from .default_agent import DEFAULT_AGENT, DEFAULT_AGENT_TYPE
from .types import AgentDefinition


class AgentRegistry:
    def __init__(self, disk_agents: list[AgentDefinition]) -> None:
        self._map: dict[str, AgentDefinition] = {}
        disk_keys = {agent.name.lower() for agent in disk_agents}

        visible_built_ins = [
            agent for agent in BUILT_IN_NAMED_AGENTS if agent.name.lower() not in disk_keys
        ]

        for agent in visible_built_ins:
            self._map[agent.name.lower()] = agent
        for agent in disk_agents:
            self._map[agent.name.lower()] = agent
        self._map[DEFAULT_AGENT_TYPE.lower()] = DEFAULT_AGENT

        self._sorted_visible = sorted(
            [*disk_agents, *visible_built_ins],
            key=lambda a: a.name,
        )
        self._sorted_all = sorted(
            [*self._sorted_visible, DEFAULT_AGENT],
            key=lambda a: a.name,
        )

    def get(self, name: str) -> AgentDefinition | None:
        return self._map.get(name.lower())

    def list(self) -> list[AgentDefinition]:
        return list(self._sorted_visible)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._sorted_all)
