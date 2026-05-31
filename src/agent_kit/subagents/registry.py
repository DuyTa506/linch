from __future__ import annotations

from .default_agent import DEFAULT_AGENT, DEFAULT_AGENT_TYPE
from .types import AgentDefinition


class AgentRegistry:
    def __init__(self, disk_agents: list[AgentDefinition]) -> None:
        self._map: dict[str, AgentDefinition] = {}
        for agent in disk_agents:
            self._map[agent.name.lower()] = agent
        self._map[DEFAULT_AGENT_TYPE.lower()] = DEFAULT_AGENT

        self._sorted_disk = sorted(disk_agents, key=lambda a: a.name)
        self._sorted_all = sorted(self._sorted_disk + [DEFAULT_AGENT], key=lambda a: a.name)

    def get(self, name: str) -> AgentDefinition | None:
        return self._map.get(name.lower())

    def list(self) -> list[AgentDefinition]:
        return list(self._sorted_disk)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._sorted_all)
