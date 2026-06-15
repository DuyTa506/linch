"""Human-in-the-loop — let the agent ask the user before it commits.

Run:
    OPENAI_API_KEY=sk-...    python examples/core/ask_user_agent.py
    DEEPSEEK_API_KEY=sk-...  python examples/core/ask_user_agent.py

The AskUser tool lets the model pause and ask the *user* a multiple-choice
question mid-run instead of guessing. You register it by passing a handler to
``Agent(ask_user=...)``; the handler renders the question however your app likes
(CLI prompt, web modal, Slack buttons) and returns the user's choice. The model
receives the answer as the tool result and continues.

What this example shows:
  - ``Agent(ask_user=handler)`` auto-registers the AskUser tool.
  - The handler returns an ``AskUserResponse`` (or a plain dict the SDK coerces).
  - **Fail-closed**: a handler that returns ``None`` / ``{}`` (e.g. the user
    dismissed the dialog) is treated as *declined*, never as silent consent.
  - **Timeout**: register ``AskUserTool(handler, timeout_s=...)`` directly so an
    unanswered prompt declines instead of hanging the run forever.
  - The handler runs off the event loop, so a *blocking* ``input()`` handler is
    fine — it won't stall concurrent tools or background workers.

``build_ask_user_agent`` is a factory so the smoke test in
``tests/test_example_interaction.py`` can drive it with a ScriptedProvider.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from linch import Agent
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore
from linch.tools import AskUserRequest, AskUserResponse, AskUserTool
from linch.tools.registry import empty_tools

SYSTEM = (
    "You are a project assistant. When a request is ambiguous, do NOT guess — call "
    "the AskUser tool with 2-4 concrete options and let the user decide. Once you have "
    "the answer, state the chosen plan in one sentence."
)


def cli_handler(request: AskUserRequest, ctx: Any) -> AskUserResponse:
    """Render each question on the terminal and read the user's choice.

    This is a *blocking* synchronous handler (it calls ``input()``). The SDK runs
    it on a worker thread, so blocking here never stalls the event loop. Return
    ``accepted=False`` to decline — the agent then proceeds with stated assumptions.
    """
    answers: dict[str, str | list[str]] = {}
    for question in request.questions:
        print(f"\n  {question.question}")
        for i, option in enumerate(question.options, 1):
            print(f"    {i}. {option.label} — {option.description}")
        raw = input("  Choose a number (blank to decline): ").strip()
        if not raw:
            return AskUserResponse(accepted=False, note="user declined")
        try:
            choice = question.options[int(raw) - 1].label
        except (ValueError, IndexError):
            return AskUserResponse(accepted=False, note="invalid choice")
        answers[question.id] = choice
    return AskUserResponse(accepted=True, answers=answers)


def build_ask_user_agent(
    handler: Any,
    *,
    provider: Any = None,
    model: str | None = None,
    timeout_s: float | None = None,
) -> Agent:
    """Build an agent that can ask the user mid-run.

    Pass ``provider`` + ``model`` (e.g. a ``ScriptedProvider``) to drive it
    deterministically. ``timeout_s`` registers the tool with an unanswered-prompt
    deadline — when set we register ``AskUserTool`` directly instead of via the
    ``ask_user=`` shortcut, which is the only difference.
    """
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider
    if timeout_s is None:
        # The common case: the shortcut wires AskUser(handler) into the registry.
        kwargs["ask_user"] = handler
        tools = empty_tools()
    else:
        tools = empty_tools(AskUserTool(handler, timeout_s=timeout_s))

    return Agent(
        model=model or "ask-user-demo",
        tools=tools,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=SYSTEM),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        **kwargs,
    )


async def main() -> None:
    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY or DEEPSEEK_API_KEY to run this example.")
        print("It shows the model calling AskUser, your handler answering, and a")
        print("fail-closed decline when the handler returns nothing.")
        return

    base_url = "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else None
    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
    )
    agent = build_ask_user_agent(cli_handler, provider=provider, model="gpt-4o-mini")
    session = await agent.session()

    print("→ Giving the agent a deliberately ambiguous task...")
    async for event in session.run("Set up testing for my project."):
        if event.type == "tool_call_start" and event.tool_name == "AskUser":
            print("  (agent is asking you to choose...)")
        elif event.type == "result":
            print(f"\n  agent: {event.final_text}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
