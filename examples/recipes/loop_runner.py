"""LoopRunner — SDK-native recurring work harness.

Run:
    DEEPSEEK_API_KEY=sk-... python examples/recipes/loop_runner.py
    OPENAI_API_KEY=sk-...   python examples/recipes/loop_runner.py

Or put credentials in ``.env`` at the repository root:

    API_KEY=sk-...
    BASE_URL=https://api.deepseek.com
    model=deepseek-chat

This example shows the V0 outer loop primitive:

  - ``LoopSpec`` describes a recurring domain of work.
  - ``LoopRunner.run_once()`` performs one host-triggered tick.
  - ``FileLoopLeaseStore`` prevents overlapping ticks across processes.
  - every tick gets a fresh session.
  - durable loop state lives in ``domains/<loop_id>/`` artifacts, not hidden
    conversation history.

The SDK does not own cron, webhooks, or daemon lifecycle. A host process calls
``run_once()`` from those systems.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from linch import (
    Agent,
    FileLoopLeaseStore,
    LoopRunner,
    LoopSpec,
    LoopTickResult,
    LoopTrigger,
    RunOptions,
)
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools

ROOT = Path(__file__).resolve().parents[2]


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def done(result: LoopTickResult, _artifacts) -> bool:
    return "LOOP_DONE" in (result.final_text or "")


def verify(result: LoopTickResult) -> bool:
    return result.report.status == "completed" and not result.report.errors


async def main() -> None:
    load_project_env()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    generic_key = os.environ.get("API_KEY")
    api_key = deepseek_key or openai_key or generic_key
    if not api_key:
        print("Set API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY in environment or project .env.")
        return
    base_url = (
        "https://api.deepseek.com"
        if deepseek_key
        else os.environ.get("LOOP_RUNNER_BASE_URL") or os.environ.get("BASE_URL")
    )
    model = os.environ.get(
        "LOOP_RUNNER_MODEL",
        os.environ.get("MODEL")
        or os.environ.get("model")
        or ("deepseek-chat" if base_url else "gpt-4o-mini"),
    )

    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(
            api_key=api_key,
            base_url=base_url,
            timeout=float(os.environ.get("LOOP_RUNNER_TIMEOUT_S", "60")),
        )
    )

    agent = Agent(
        model=model,
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a concise maintenance agent. Answer plainly. "
                "When the requested tick is complete, include the token LOOP_DONE."
            ),
        ),
    )
    spec = LoopSpec(
        id="loop-runner-demo",
        charter=(
            "Maintain a tiny recurring status domain. Each tick should inspect the "
            "loop context and produce one concise status update."
        ),
        prompt="Write a one-sentence status update for this loop tick.",
        root=os.environ.get("LINCH_LOOP_ROOT", "domains"),
        run_options=RunOptions(max_output_tokens=120),
        session_meta={"example": "loop_runner"},
    )
    runner = LoopRunner(
        agent,
        leases=FileLoopLeaseStore(root=spec.root),
        verify=verify,
        done_predicate=done,
    )

    try:
        result = await asyncio.wait_for(
            runner.run_once(
                spec,
                LoopTrigger(
                    source="manual",
                    payload="Example script was run by a developer.",
                    metadata={"example": "examples/recipes/loop_runner.py"},
                ),
            ),
            timeout=float(os.environ.get("LOOP_RUNNER_TIMEOUT_S", "60")),
        )
    except asyncio.TimeoutError:
        print("LoopRunner tick timed out; check BASE_URL/model credentials and retry.")
        await agent.close()
        await _close_provider(provider)
        return

    print(f"loop_id: {result.loop_id}")
    print(f"iteration: {result.iteration}")
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}")
    print(f"done: {result.done}")
    print(f"final: {result.final_text}")
    print("artifacts:")
    for path in result.artifact_paths:
        print(f"  {path}")

    await agent.close()
    await _close_provider(provider)


async def _close_provider(provider: object) -> None:
    close = getattr(provider, "aclose", None)
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), timeout=5)
    except asyncio.TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
