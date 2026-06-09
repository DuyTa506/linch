"""Policy-aware Bash execution with approval rules and Docker restrictions.

Run:
    OPENAI_API_KEY=sk-... python3 examples/core/policy_aware_execution.py

This example keeps permission decisions in linch rules while running approved
Bash commands inside a Docker container with opt-in runtime restrictions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from linch import Agent
from linch.errors import ToolExecutionError
from linch.permissions import BashRule, PathRule
from linch.sessions import InMemorySessionStore
from linch.tools.execution import DockerBackend
from linch.tools.registry import tools_from_defaults

ROOT = Path(__file__).resolve().parents[2]


def _make_agent() -> Agent:
    try:
        backend = DockerBackend(
            network="none",
            read_only_root=True,
            workspace_mount="rw",
            tmpfs=("/tmp:rw,noexec,nosuid,nodev,size=64m",),
            forward_env=(),
        )
    except ToolExecutionError as exc:
        raise SystemExit(str(exc)) from exc

    return Agent(
        model="gpt-5",
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        cwd=str(ROOT),
        execution_backend=backend,
        permissions={
            "mode": "acceptEdits",
            "rules": [
                BashRule(patterns=["rm -rf*", "sudo *", "curl *", "wget *"], decision="deny"),
                PathRule(paths=[str(ROOT / "**")], decision="allow"),
                PathRule(paths=["/**"], decision="deny"),
            ],
        },
        system_prompt=(
            "You are a concise engineering assistant. "
            "Use Bash only when it directly helps answer the request."
        ),
    )


async def main() -> None:
    agent = _make_agent()
    session = await agent.session()

    async for event in session.run(
        "Run `pwd` and `python --version`, then explain what the sandbox settings imply."
    ):
        if event.type == "assistant":
            for block in event.message.content:
                if block.type == "text":
                    print(block.text, end="")
        elif event.type == "tool_call_start":
            print(f"\n[{event.tool_name}] {event.summary}")
        elif event.type == "tool_call_end" and event.is_error:
            print(f"\nERROR: {event.result[:160]}")
        elif event.type == "result":
            break

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
