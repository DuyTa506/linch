"""Tiny external-consumer fixture: core and extensions use explicit APIs."""

from linch import (
    Agent,
    FeatureFlags,
    RunContract,
    ToolProgressEvent,
    build_run_contract,
    workspace_tools,
)
from linch.ext.deep_agent import DEEP_AGENT_PROFILES, DeepAgentProfile


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
