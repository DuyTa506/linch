"""Skills usage example.

Run:
    python3 examples/integrations/skills_usage.py

Demonstrates:
  1. Creating a project skill at .agent_kit/skills/<name>/SKILL.md
  2. Loading skills through Agent(features=FeatureFlags(skills=True))
  3. Invoking the skill through the built-in Skill tool
  4. Observing skill lifecycle events

This example uses a fake provider so it runs without an API key.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from agent_kit import Agent, BaseProvider, FeatureFlags, Usage
from agent_kit.sessions import InMemorySessionStore


SKILL_MD = """---
description: Convert rough notes into a short stakeholder update.
when_to_use: Use when the user asks to summarize progress for stakeholders.
arguments:
  - audience
argument_hint: audience name, followed by the source notes
---
# Stakeholder Update

Write a concise update for $audience.

Use these source notes:

$ARGUMENTS

End with:

- Status:
- Risk:
- Next step:
"""


class FakeProvider(BaseProvider):
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> Any:
        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}

        if len(self.requests) == 1:
            yield {"type": "tool_use_start", "id": "skill-1", "name": "Skill"}
            yield {
                "type": "tool_use_input_delta",
                "id": "skill-1",
                "json_delta": json.dumps(
                    {
                        "skill": "stakeholder-update",
                        "args": "leadership launch is on track, docs need final review",
                    }
                ),
            }
            yield {"type": "tool_use_end", "id": "skill-1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return

        yield {"type": "text_delta", "text": "I would now follow the skill instructions."}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def write_project_skill(root: Path) -> None:
    skill_dir = root / ".agent_kit" / "skills" / "stakeholder-update"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_project_skill(root)

        agent = Agent(
            model="fake-model",
            provider=FakeProvider(),
            cwd=str(root),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            features=FeatureFlags(skills=True, subagents=False, mcp=False, filesystem=False),
            result_offload=None,
        )
        session = await agent.session()

        async for event in session.run("Use the stakeholder update skill."):
            if event.type == "skills_loaded":
                print("skills loaded:", [item["name"] for item in event.skills])
            elif event.type == "skill_invoked":
                print("skill invoked:", event.name)
                print("args:", event.args)
            elif event.type == "tool_call_end" and event.tool_name == "Skill":
                print("\nreturned skill instructions:")
                print(event.result)
            elif event.type == "skill_completed":
                print("skill completed:", event.name, "is_error=", event.is_error)
            elif event.type == "assistant":
                for block in event.message.content:
                    if block.type == "text":
                        print("\nassistant:", block.text)

        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
