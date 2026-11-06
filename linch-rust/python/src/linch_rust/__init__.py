"""Minimal asyncio client for the Linch Rust core.

It spawns the `linch-server` sidecar and speaks its JSON-RPC protocol over
stdio; everything inside a turn runs in the server, which calls back into
this process only for the host tools and callbacks an agent declared::

    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup])
        run = await agent.run("session-1", "Find ada@example.com")
        async for envelope in run:
            print(envelope.seq, envelope.event)
"""

from ._binary import find_server
from .agent import Agent, Allow, Decision, Deny
from .callbacks import Callback, FailMode, Point, callback
from .config import AgentConfig, DeepAgent, DeepAgentProfile, Filesystem, Profile, Workers
from .errors import (
    AgentNotFound,
    ConnectionClosed,
    ContractMismatch,
    LinchError,
    NotSuspended,
    OpenError,
    PromptBlocked,
    ProtocolError,
    RpcError,
    RunBusy,
    RunError,
    RunNotFound,
    RunTerminal,
    SessionBusy,
)
from .events import (
    AssistantEvent,
    CompactionEvent,
    ContextBuildEvent,
    Envelope,
    ErrorEvent,
    Event,
    HookEvent,
    InitEvent,
    LoopGuardEvent,
    PartialAssistantEvent,
    PermissionRequestEvent,
    ResultEvent,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolProgressEvent,
    UnknownEvent,
    UsageEvent,
    UserEvent,
)
from .run import Run
from .server import Server
from .tools import HostTool, Scope, tool

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentNotFound",
    "Allow",
    "AssistantEvent",
    "Callback",
    "CompactionEvent",
    "ConnectionClosed",
    "ContextBuildEvent",
    "ContractMismatch",
    "Decision",
    "DeepAgent",
    "DeepAgentProfile",
    "Deny",
    "Envelope",
    "ErrorEvent",
    "Event",
    "FailMode",
    "Filesystem",
    "HookEvent",
    "HostTool",
    "InitEvent",
    "LinchError",
    "LoopGuardEvent",
    "NotSuspended",
    "OpenError",
    "PartialAssistantEvent",
    "PermissionRequestEvent",
    "Point",
    "Profile",
    "PromptBlocked",
    "ProtocolError",
    "ResultEvent",
    "RpcError",
    "Run",
    "RunBusy",
    "RunError",
    "RunNotFound",
    "RunTerminal",
    "Scope",
    "Server",
    "SessionBusy",
    "SuspendedEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "ToolProgressEvent",
    "UnknownEvent",
    "UsageEvent",
    "UserEvent",
    "Workers",
    "callback",
    "find_server",
    "tool",
]
