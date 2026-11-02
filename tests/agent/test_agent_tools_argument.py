import pytest

from linch import Agent, ConfigError
from tests.support.providers import ScriptedProvider


def test_agent_rejects_a_plain_tool_list() -> None:
    with pytest.raises(ConfigError, match="ToolRegistry"):
        Agent(model="m", provider=ScriptedProvider([]), tools=[object()])  # type: ignore[arg-type]
