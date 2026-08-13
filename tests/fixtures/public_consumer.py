"""Tiny external-consumer fixture: only the top-level Linch API is allowed."""

from linch import (
    DEEP_AGENT_PROFILES,
    Agent,
    DeepAgentProfile,
    FeatureFlags,
    RunContract,
    ToolProgressEvent,
    build_run_contract,
    workspace_tools,
)


def make_agent() -> Agent:
    """Construct a workspace agent without importing Linch implementation paths."""

    return Agent(
        model="fixture-model",
        tools=workspace_tools(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
    )


def make_contract() -> RunContract:
    return build_run_contract(primary_model="fixture-model")


__all__ = [
    "DEEP_AGENT_PROFILES",
    "DeepAgentProfile",
    "ToolProgressEvent",
    "make_agent",
    "make_contract",
]
